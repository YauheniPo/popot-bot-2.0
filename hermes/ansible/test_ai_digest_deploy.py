"""Deployment contracts for the cron-backed AI digest."""

import json
from pathlib import Path
import unittest

try:
    from ansible.plugins.filter.core import FilterModule
    from jinja2 import Environment
except ImportError:  # pragma: no cover - full rendering needs Ansible's controller env
    FilterModule = None
    Environment = None

ANSIBLE = Path(__file__).parent
PLAYBOOK = (ANSIBLE / "playbook.yml").read_text()
RUNTIME = (ANSIBLE / "tasks" / "runtime.yml").read_text()
ENV = (ANSIBLE / "templates" / "hermes.env.j2").read_text()


class DigestDeployTests(unittest.TestCase):
    def test_repository_skills_are_copied_and_existing_paths_preserved(self):
        self.assertIn('src: "{{ playbook_dir }}/../skills/"', PLAYBOOK)
        self.assertIn("- --exclude=__pycache__\n", PLAYBOOK)
        for pattern in (".env", ".env.*", ".envrc", "dotenv", "env", "env.*",
                        "*.secret", "*.key", "*.pem", "node_modules"):
            self.assertIn(f"- --exclude={pattern}\n", PLAYBOOK)
        self.assertIn("hermes_bundle_dir ~ '/skills'", RUNTIME)
        self.assertIn("hermes_existing_config.get('skills', {}).get('external_dirs', [])", RUNTIME)

    def test_paths_follow_existing_identity_without_managing_cron_jobs(self):
        self.assertIn("hermes_home ~ '/ops/news'", ENV)
        self.assertIn("hermes_workspace ~ '/digests'", ENV)
        self.assertIn("hermes_bundle_dir ~ '/skills/ai_digest'", ENV)
        self.assertIn("hermes_searxng_url", ENV)
        self.assertNotIn("Ensure the repository-owned AI digest cron job", RUNTIME)

    def test_digest_search_url_is_rendered_once_when_configured(self):
        self.assertIn("prefers AI_DIGEST_SEARCH_URL and falls back to SEARXNG_URL", ENV)
        self.assertEqual(ENV.count("AI_DIGEST_SEARCH_URL={{"), 1)
        runtime = (ANSIBLE / "tasks" / "runtime.yml").read_text()
        self.assertIn('owner: "{{ hermes_user }}"', runtime)
        self.assertIn('group: "{{ hermes_user }}"', runtime)
        self.assertIn('mode: "0600"', runtime)
        if Environment is None:
            self.skipTest("Ansible controller dependencies are not installed")
        template = Environment()
        template.filters.update(FilterModule().filters())
        rendered = template.from_string(ENV).render(
            hermes_secret_env={}, hermes_home="/home/hermes",
            hermes_workspace="/home/hermes/workspace", hermes_bundle_dir="/opt/hermes",
            hermes_searxng_url="https://search.example.test",
            vps_browser={"launch_args": ""},
            vps_deploy={"features": {"workspace_ui": False}},
        )
        values = [line.partition("=")[2] for line in rendered.splitlines()
                  if line.startswith("AI_DIGEST_SEARCH_URL=")]
        self.assertEqual(len(values), 1)
        self.assertEqual(json.loads(values[0]), "https://search.example.test")


if __name__ == "__main__":
    unittest.main()
