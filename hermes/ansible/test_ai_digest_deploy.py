"""Deployment contracts for the cron-backed AI digest."""

from pathlib import Path
import unittest

ANSIBLE = Path(__file__).parent
PLAYBOOK = (ANSIBLE / "playbook.yml").read_text()
RUNTIME = (ANSIBLE / "tasks" / "runtime.yml").read_text()
ENV = (ANSIBLE / "templates" / "hermes.env.j2").read_text()


class DigestDeployTests(unittest.TestCase):
    def test_repository_skills_are_copied_and_existing_paths_preserved(self):
        self.assertIn('src: "{{ playbook_dir }}/../skills/"', PLAYBOOK)
        self.assertIn("- --exclude=__pycache__\n", PLAYBOOK)
        for pattern in (".env", ".env.*", "env", "env.*", "*.secret", "*.key", "*.pem", "node_modules"):
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
        self.assertEqual(ENV.count("AI_DIGEST_SEARCH_URL="), 1)


if __name__ == "__main__":
    unittest.main()
