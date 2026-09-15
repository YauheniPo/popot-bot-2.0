"""Regression checks for the UFW SSH lock-down task."""

from pathlib import Path
import re
import unittest


NETWORK_TASKS = Path(__file__).with_name("tasks") / "network.yml"


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


if __name__ == "__main__":
    unittest.main()
