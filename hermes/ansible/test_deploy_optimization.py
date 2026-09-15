"""Regression checks for the fast Hermes deployment paths."""

from pathlib import Path
import unittest


ANSIBLE = Path(__file__).parent
PLAYBOOK = (ANSIBLE / "playbook.yml").read_text()
SERVICES = (ANSIBLE / "tasks" / "services.yml").read_text()


class DeployOptimizationTests(unittest.TestCase):
    def test_versioned_directories_use_rsync_synchronization(self) -> None:
        self.assertIn("ansible.posix.synchronize:", PLAYBOOK)
        self.assertIn("checksum: true", PLAYBOOK)
        self.assertNotIn("--chown=root:root", PLAYBOOK)
        self.assertIn("Normalize synchronized bundle ownership", PLAYBOOK)

    def test_fast_modes_are_explicit_and_validate_source_is_current(self) -> None:
        self.assertIn("hermes_deploy_mode in ['full', 'config-only', 'runtime-only']", PLAYBOOK)
        self.assertIn("Fast deployment modes require the installed Hermes source", PLAYBOOK)
        self.assertIn("hermes_deploy_mode == 'full'", PLAYBOOK)

    def test_tailscale_serve_validation_is_feature_gated(self) -> None:
        self.assertIn("Validate Tailscale Serve settings", PLAYBOOK)
        self.assertIn("when: vps_deploy.features.tailscale | bool", PLAYBOOK)

    def test_services_restart_only_via_notified_handlers(self) -> None:
        self.assertNotIn("state: restarted", SERVICES)
        self.assertIn("meta: flush_handlers", SERVICES)
        self.assertIn("restart Hermes gateway", PLAYBOOK)
        self.assertIn("restart managed observability services", PLAYBOOK)


if __name__ == "__main__":
    unittest.main()
