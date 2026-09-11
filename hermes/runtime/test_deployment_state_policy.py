"""Static contracts for managed-versus-personal Hermes deployment state."""

from __future__ import annotations

import getpass
import grp
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


HERMES_ROOT = Path(__file__).resolve().parents[1]


class DeploymentStatePolicyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is required")
    def test_devops_instructions_preserve_notes_and_update_idempotently(self) -> None:
        playbook = yaml.safe_load((HERMES_ROOT / "ansible" / "playbook.yml").read_text())
        task = next(
            (task for task in playbook[0]["tasks"]
             if task.get("name") == "Publish managed Azure DevOps and SonarQube instructions"),
            None,
        )
        self.assertIsNotNone(task, "Deployment must publish persistent DevOps instructions")
        settings = yaml.safe_load((HERMES_ROOT / "config" / "vps-defaults.yml").read_text())

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            templates = temporary / "templates"
            templates.mkdir()
            shutil.copyfile(
                HERMES_ROOT / "ansible" / "templates" / "devops-access.md.j2",
                templates / "devops-access.md.j2",
            )
            workspace = temporary / "workspace"
            workspace.mkdir()
            instructions = workspace / "AGENTS.md"
            personal = "# Personal notes\nKeep the owner's deployment preferences.\n"
            instructions.write_text(personal)
            local_playbook = temporary / "check.yml"
            variables = {
                "vps_integrations": settings["vps_integrations"],
                "hermes_workspace": str(workspace),
                "hermes_user": getpass.getuser(),
                "hermes_group": grp.getgrgid(os.getgid()).gr_name,
                "hermes_secret_env": {
                    "AZURE_DEVOPS_EXT_PAT": "test-only-azure-secret",
                    "SONAR_TOKEN": "test-only-sonar-secret",
                },
            }
            # Production uses the Hermes user as its group; the local test user
            # may instead have a primary group such as macOS "staff".
            local_task = {**task, "ansible.builtin.blockinfile": {
                **task["ansible.builtin.blockinfile"], "group": "{{ hermes_group }}",
            }}

            def apply() -> subprocess.CompletedProcess:
                local_playbook.write_text(yaml.safe_dump([{
                    "name": "Check persistent DevOps instructions",
                    "hosts": "localhost", "gather_facts": False,
                    "vars": variables, "tasks": [local_task],
                }]))
                result = subprocess.run(
                    ["ansible-playbook", "-i", "localhost,", "-c", "local", str(local_playbook)],
                    env={**os.environ, "ANSIBLE_CONFIG": str(HERMES_ROOT / "ansible" / "ansible.cfg")},
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            apply()
            initial = instructions.read_text()
            self.assertIn(personal, initial)
            self.assertIn("https://dev.azure.com/YauheniPo", initial)
            self.assertIn("YauheniPo_popot-bot-2.0", initial)
            for name, secret in variables["hermes_secret_env"].items():
                self.assertIn(name, initial)
                self.assertNotIn(secret, initial)
            self.assertEqual(instructions.stat().st_mode & 0o777, 0o600)

            variables["hermes_secret_env"] = {}
            second = apply()
            self.assertIn("changed=0", second.stdout)
            self.assertEqual(instructions.read_text(), initial)

            variables["vps_integrations"]["azure_devops"]["project"] = "another-project"
            apply()
            updated = instructions.read_text()
            self.assertIn(personal, updated)
            self.assertIn("another-project", updated)
            self.assertEqual(updated.count("<!-- BEGIN ANSIBLE MANAGED DEVOPS ACCESS -->"), 1)
            self.assertEqual(updated.count("<!-- END ANSIBLE MANAGED DEVOPS ACCESS -->"), 1)

    def test_llm_overlay_merges_only_explicit_nested_keys(self) -> None:
        runtime_tasks = (HERMES_ROOT / "ansible" / "tasks" / "runtime.yml").read_text()

        self.assertIn("combine(hermes_managed_config, recursive=true, list_merge='replace')", runtime_tasks)
        variables = (HERMES_ROOT / "ansible" / "group_vars" / "all" / "vars.yml").read_text()
        self.assertIn("combine(vps_hermes.config.managed_overlay", variables)

    def test_config_only_deploy_requires_a_verified_full_backup(self) -> None:
        playbook = (HERMES_ROOT / "ansible" / "playbook.yml").read_text()

        self.assertIn("pre-config-deploy-", playbook)
        self.assertIn("Create the mandatory full config-only deployment backup", playbook)
        self.assertIn("Verify the config-only deployment backup contents", playbook)
        self.assertIn("when: hermes_source_update_required | bool", playbook)

    def test_workspace_agents_uses_selective_managed_block_reconciliation(self) -> None:
        playbook = (HERMES_ROOT / "ansible" / "playbook.yml").read_text()
        scheduled_backup = (HERMES_ROOT / "ops" / "backup.sh").read_text()

        self.assertIn("runtime/manage-workspace-agents.py", playbook)
        self.assertIn("Reconcile repository-owned host-administration instructions", playbook)
        self.assertIn("operator-state/workspace-AGENTS.md", playbook)
        self.assertIn("operator-state", scheduled_backup)
        self.assertIn("workspace/AGENTS.md", scheduled_backup)
        self.assertIn("Hermes workspace must be a non-empty absolute safe path", scheduled_backup)
        self.assertIn("Hermes backup run-as user does not exist", scheduled_backup)
        self.assertIn('rm -f -- "${operator_state_agents}"', scheduled_backup)

    def test_final_config_check_runs_before_gateway_install(self) -> None:
        services = (HERMES_ROOT / "ansible" / "tasks" / "services.yml").read_text()

        check_position = services.index("Validate the final managed Hermes configuration")
        gateway_position = services.index("Install and start the Hermes system gateway")
        self.assertLess(check_position, gateway_position)
        self.assertIn("ANSIBLE MANAGED RESPONSE LANGUAGE", services)

    def test_broken_existing_install_never_bypasses_backup(self) -> None:
        deploy_runtime = (HERMES_ROOT / "deploy" / "runtime.sh").read_text()

        self.assertIn("refusing to update without a verified backup", deploy_runtime)
        self.assertIn("state exists but its CLI is missing", deploy_runtime)
        self.assertNotIn("skipping backup of an unusable installation", deploy_runtime)


if __name__ == "__main__":
    unittest.main()
