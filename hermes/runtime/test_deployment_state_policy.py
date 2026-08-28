"""Static contracts for managed-versus-personal Hermes deployment state."""

from __future__ import annotations

from pathlib import Path
import unittest


HERMES_ROOT = Path(__file__).resolve().parents[1]


class DeploymentStatePolicyTests(unittest.TestCase):
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
