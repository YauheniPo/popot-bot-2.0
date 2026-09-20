"""Regression tests for selective workspace/AGENTS.md ownership."""

from __future__ import annotations

import importlib.util
import contextlib
import io
import os
import re
import runpy
import shlex
import shutil
import subprocess
from pathlib import Path
import tempfile
import stat
import unittest
from unittest import mock

import yaml
from jinja2 import Environment, StrictUndefined

MODULE_PATH = Path(__file__).with_name("manage-workspace-agents.py")
SPEC = importlib.util.spec_from_file_location("manage_workspace_agents", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
manage_workspace_agents = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage_workspace_agents)


class ManageWorkspaceAgentsTests(unittest.TestCase):
    def test_container_bootstrap_instruction_step_migrates_and_refreshes_without_clobbering_notes(self):
        repo = MODULE_PATH.parents[1]
        bootstrap = (repo / "docker/20-local-bootstrap").read_text()
        step = bootstrap[bootstrap.index("if ! python3 /opt/hermes-local/manage-workspace-agents.py"):bootstrap.index("\nas_hermes()")]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            assets, state = root / "assets", root / "state"
            assets.mkdir()
            (state / "workspace").mkdir(parents=True)
            for source, dest in ((MODULE_PATH, "manage-workspace-agents.py"),
                                 (repo / "instructions/common.md", "common-AGENTS.md"),
                                 (repo / "docker/AGENTS.md", "container-admin-AGENTS.md"),
                                 (repo / "runtime/legacy/container-instructions.md", "legacy-container-AGENTS.md")):
                shutil.copyfile(source, assets / dest)
            target = state / "workspace/AGENTS.md"
            target.write_text((assets / "legacy-container-AGENTS.md").read_text() + "\nPersonal note\n")
            marker = state / "workspace/.hermes-host-admin-instructions"
            marker.touch()
            # Only adapt installed asset paths and OS identity, not bootstrap behavior.
            command = 'state_dir=' + shlex.quote(str(state)) + '\n' + step.replace(
                "/opt/hermes-local", str(assets)).replace("chown hermes:hermes", f"chown {os.getuid()}:{os.getgid()}")
            for policy in ("Shared policy v1", "Shared policy v2", "Shared policy v2"):
                (assets / "common-AGENTS.md").write_text(policy)
                previous_time = target.stat().st_mtime_ns
                result = subprocess.run(["sh", "-eu", "-c", command], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(policy, target.read_text())
                self.assertIn("Personal note", target.read_text())
                self.assertNotIn("# Delivery discipline", target.read_text())
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                if "unchanged" in result.stdout:
                    self.assertEqual(target.stat().st_mtime_ns, previous_time)
            self.assertIn("unchanged", result.stdout)
            self.assertTrue(marker.exists())  # Left inert, never used to clobber the file.

            # A partial image may lack instruction sources. Bootstrap must
            # leave a usable empty file instead of aborting on chown/chmod.
            target.unlink()
            for source in (assets / "common-AGENTS.md", assets / "container-admin-AGENTS.md",
                           assets / "legacy-container-AGENTS.md"):
                source.unlink()
            result = subprocess.run(["sh", "-eu", "-c", command], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(target.is_file())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_shared_layers_replace_old_policy_and_preserve_personal_and_integration_blocks(self):
        personal = "# Personal\nKeep this note.\n\n<!-- BEGIN MANAGED GITHUB WORKFLOW -->\nGitHub\n<!-- END MANAGED GITHUB WORKFLOW -->\n"
        old = manage_workspace_agents.reconcile(personal, "Old VPS policy", present=True)
        for environment in ("VPS administration disabled", "Container only"):
            with self.subTest(environment=environment):
                result = manage_workspace_agents.reconcile_layers(old, "Common rules", environment)
                self.assertNotIn("Old VPS policy", result)
                self.assertNotIn(manage_workspace_agents.BEGIN_MARKER, result)
                self.assertIn(personal, result)
                self.assertEqual(result.count("Common rules"), 1)
                self.assertEqual(result.count(environment), 1)
                self.assertEqual(manage_workspace_agents.reconcile_layers(result, "Common rules", environment), result)
                updated = manage_workspace_agents.reconcile_layers(result, "Common v2", "Different environment")
                self.assertNotIn("Common rules", updated)
                self.assertNotIn(environment, updated)
                self.assertIn(personal, updated)

    def test_legacy_container_migration_removes_only_exact_known_prefix(self):
        legacy = "# Old container policy\nKnown rules."
        for existing, expected in ((legacy, ""), (legacy + "\n\nPersonal tail", "Personal tail"),
                                   (legacy + " locally edited", legacy + " locally edited"),
                                   ("My custom policy", "My custom policy")):
            with self.subTest(existing=existing):
                result = manage_workspace_agents.reconcile_layers(
                    existing, "Common", "Container", legacy_sources=[legacy])
                if expected:
                    self.assertIn(expected, result)
                if not expected.startswith(legacy):
                    self.assertNotIn(legacy, result)

    def test_layered_cli_preserves_backup_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target, backup = root / "AGENTS.md", root / "backup.md"
            common, environment, legacy = (root / name for name in ("common.md", "env.md", "legacy.md"))
            common.write_text("Shared policy")
            environment.write_text("No host administration")
            legacy.write_text("Old container policy")
            target.write_text("Old container policy\n\nMy notes\n")
            argv = ["manage-workspace-agents.py", "--target", str(target),
                    "--managed-source", str(environment), "--common-source", str(common),
                    "--legacy-source", str(legacy), "--backup-copy", str(backup)]
            for _ in range(2):
                with mock.patch("sys.argv", argv):
                    self.assertEqual(manage_workspace_agents.main(), 0)
                self.assertEqual(target.read_text(), backup.read_text())
                self.assertIn("Shared policy", target.read_text())
                self.assertIn("My notes", target.read_text())
                self.assertNotIn("Old container policy", target.read_text())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_malformed_layer_markers_fail_without_writing(self):
        for marker in ("HERMES MANAGED COMMON", "HERMES MANAGED ENVIRONMENT"):
            for existing in (f"<!-- BEGIN {marker} -->\nPersonal", f"<!-- END {marker} -->",
                             f"<!-- END {marker} -->\n<!-- BEGIN {marker} -->"):
                with self.subTest(existing=existing):
                    with self.assertRaises((manage_workspace_agents.ManagedBlockError, ValueError)):
                        manage_workspace_agents.reconcile_layers(existing, "Common", "Environment")

    def test_both_deployments_ship_the_same_common_source(self):
        root = MODULE_PATH.parents[1]
        playbook = (root / "ansible/playbook.yml").read_text()
        dockerfile = (root / "docker/Dockerfile").read_text()
        bootstrap = (root / "docker/20-local-bootstrap").read_text()
        self.assertIn("../instructions/common.md", playbook)
        self.assertIn("hermes/instructions/common.md", dockerfile)
        self.assertIn("--common-source", playbook)
        self.assertIn("--common-source", bootstrap)
        self.assertIn("manage-workspace-agents.py", dockerfile)
        self.assertNotIn("legacy_host_admin_instruction_marker", bootstrap)

    def test_instruction_source_map_points_to_existing_repository_files(self) -> None:
        source = (MODULE_PATH.parents[1] / "ansible/AGENTS.md").read_text()
        paths = re.findall(r"`(hermes/[^`]+)`", source)
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue((MODULE_PATH.parents[2] / path).is_file()
                                or self._is_deliberately_unversioned(MODULE_PATH.parents[2], path))

    @staticmethod
    def _is_deliberately_unversioned(root: Path, path: str) -> bool:
        """Accept a documented path that Git deliberately ignores.

        Encrypted Vault and credential paths must exist on a deployed host but
        never in the repository, so the map check has to tell a deliberate
        ignore apart from a dangling reference. Git is the authority for that.
        """
        try:
            result = subprocess.run(["git", "-C", str(root), "check-ignore", "--quiet", "--", path],
                                    capture_output=True, check=False)
        except OSError:
            return False
        return result.returncode == 0

    def test_searxng_instructions_do_not_invent_an_unconfigured_endpoint(self) -> None:
        template = Environment(undefined=StrictUndefined).from_string(
            (MODULE_PATH.parents[1] / "ansible/templates/searxng-access.md.j2").read_text()
        )
        for url in ("", "   "):
            with self.subTest(url=url):
                rendered = template.render(vps_web={"searxng_url": url})
                self.assertIn("not configured", rendered)
                self.assertNotIn("curl", rendered)
                self.assertNotIn("/search", rendered)
        for url in ("http://127.0.0.1:9876", "https://search.example.test/base/"):
            with self.subTest(url=url):
                rendered = template.render(vps_web={"searxng_url": url})
                self.assertIn(f"'{url.rstrip('/')}/search'", rendered)
                self.assertNotIn("not configured", rendered)

    def test_repository_policy_update_keeps_other_blocks_and_personal_notes(self) -> None:
        source = (MODULE_PATH.parents[1] / "ansible/AGENTS.md").read_text()
        other_blocks = "\n".join(
            f"<!-- BEGIN {name} -->\nKeep {name}\n<!-- END {name} -->"
            for name in ("MANAGED VAULT ENVIRONMENT NAMES", "MANAGED GITHUB WORKFLOW",
                         "ANSIBLE MANAGED DEVOPS ACCESS", "ANSIBLE MANAGED SEARXNG ACCESS",
                         "ANSIBLE MANAGED DELEGATION POLICY")
        )
        personal = "# Personal\nKeep my project-specific instructions."
        existing = manage_workspace_agents.reconcile(
            other_blocks + "\n\n" + personal, "Old repository policy", present=True
        )
        result = manage_workspace_agents.reconcile(existing, source, present=True)
        self.assertNotIn("Old repository policy", result)
        self.assertEqual(result.count(source.strip()), 1)
        self.assertIn(other_blocks + "\n\n" + personal, result)
        self.assertEqual(manage_workspace_agents.reconcile(result, source, present=True), result)

    def run_cli(self, target, source, backup=None, state="present", common=None, legacy=()):
        argv = ["manage-workspace-agents.py", "--target", str(target),
                "--managed-source", str(source), "--state", state]
        if backup is not None:
            argv.extend(["--backup-copy", str(backup)])
        if common is not None:
            argv.extend(["--common-source", str(common)])
        for path in legacy:
            argv.extend(["--legacy-source", str(path)])
        with mock.patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            return manage_workspace_agents.main()

    def test_shared_source_rejects_empty_marker_bearing_and_disabled_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "AGENTS.md"
            target.write_text("Personal notes\n")
            source = root / "env.md"
            source.write_text("VPS policy\n")
            for content in ("   ", "<!-- BEGIN HERMES MANAGED COMMON -->\nbody\n",
                            "body\n<!-- END HERMES MANAGED COMMON -->"):
                common = root / "common.md"
                common.write_text(content)
                with self.subTest(content=content[:20]):
                    self.assertEqual(self.run_cli(target, source, common=common), 2)
                    self.assertEqual(target.read_text(), "Personal notes\n")
            # --state only means something for the single-source form.
            common = root / "common.md"
            common.write_text("Shared policy\n")
            self.assertEqual(self.run_cli(target, source, common=common, state="absent"), 2)

    def test_legacy_source_requires_the_shared_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "AGENTS.md"
            target.write_text("Personal notes\n")
            source = root / "env.md"
            source.write_text("VPS policy\n")
            legacy = root / "legacy.md"
            legacy.write_text("Old container policy\n")
            self.assertEqual(self.run_cli(target, source, legacy=[legacy]), 2)
            self.assertEqual(target.read_text(), "Personal notes\n")

    def test_shared_source_composes_both_layers_and_migrates_legacy_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "AGENTS.md"
            target.write_text("Old container policy\n\nMy notes\n")
            managed = root / "env.md"
            managed.write_text("VPS policy\n")
            common = root / "common.md"
            common.write_text("Shared policy\n")
            legacy = root / "legacy.md"
            legacy.write_text("Old container policy\n")
            self.assertEqual(self.run_cli(target, managed, common=common, legacy=[legacy]), 0)
            written = target.read_text()
            self.assertIn("Shared policy", written)
            self.assertIn("VPS policy", written)
            self.assertIn("My notes", written)
            self.assertNotIn("Old container policy", written)
            # Re-running is a no-op.
            self.assertEqual(self.run_cli(target, managed, common=common, legacy=[legacy]), 0)
            self.assertEqual(target.read_text(), written)

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
            expected_mtimes = (target.stat().st_mtime_ns, backup.stat().st_mtime_ns)
            self.assertEqual(self.run_cli(target, source, backup), 0)
            actual_mtimes = (target.stat().st_mtime_ns, backup.stat().st_mtime_ns)
            self.assertEqual(actual_mtimes, expected_mtimes)
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
