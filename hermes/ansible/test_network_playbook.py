"""Regression checks for the UFW SSH lock-down task."""

from pathlib import Path
import re
import unittest


NETWORK_TASKS = Path(__file__).with_name("tasks") / "network.yml"
PLAYBOOK = Path(__file__).with_name("playbook.yml")


class NetworkPlaybookTests(unittest.TestCase):
    def test_private_ssh_rule_has_empty_ufw_fallback(self) -> None:
        """A fresh UFW ruleset cannot accept ``ufw insert 1``."""
        tasks = NETWORK_TASKS.read_text()

        self.assertRegex(
            tasks,
            re.compile(
                r"if ufw status numbered \| grep -q '\^\\\['; then\n"
                r"\s+ufw insert 1 allow in on \{\{ vps_network\.tailscale\.interface \}\}"
                r" to any port \{\{ vps_network\.ssh_port \}\} proto tcp"
                r" comment 'Hermes private SSH'\n"
                r"\s+else\n"
                r"\s+ufw allow in on \{\{ vps_network\.tailscale\.interface \}\}"
                r" to any port \{\{ vps_network\.ssh_port \}\} proto tcp"
                r" comment 'Hermes private SSH'\n"
                r"\s+fi\n"
                r"\s+ufw insert 2 deny in to any port \{\{ vps_network\.ssh_port \}\}"
                r" proto tcp comment 'Hermes deny public SSH'"
            ),
        )

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
