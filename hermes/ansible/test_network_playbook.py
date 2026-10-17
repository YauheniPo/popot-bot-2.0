"""Regression checks for the UFW SSH lock-down task."""

from pathlib import Path
import unittest


NETWORK_TASKS = Path(__file__).with_name("tasks") / "network.yml"
PLAYBOOK = Path(__file__).with_name("playbook.yml")


class NetworkPlaybookTests(unittest.TestCase):
    def test_private_ssh_reconciliation_uses_verified_runtime_helper(self) -> None:
        import yaml
        tasks = yaml.safe_load(NETWORK_TASKS.read_text())
        task = next(t for t in tasks if t.get('name') == 'Reconcile private-only SSH rules and firewall policies')
        self.assertIn('{{ hermes_bundle_dir }}/runtime/reconcile-firewall.py', task['ansible.builtin.command']['argv'])
        self.assertEqual(task['when'], 'hermes_lock_public_ssh | bool')
        self.assertIn('firewall-changed=true', task['changed_when'])
        self.assertFalse(any('ansible.builtin.shell' in t for t in tasks))

    def test_public_ssh_lockdown_requires_the_controller_port_to_match(self) -> None:
        playbook = PLAYBOOK.read_text()

        self.assertIn(
            "Refuse public SSH lock-down when Ansible uses another port",
            playbook,
        )
        self.assertIn(
            "(ansible_port | default(22, true) | int) == (vps_network.ssh_port | int)",
            playbook,
        )
        self.assertIn("when: hermes_lock_public_ssh | bool", playbook)


if __name__ == "__main__":
    unittest.main()
