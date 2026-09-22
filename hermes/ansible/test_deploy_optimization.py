"""Regression checks for the fast Hermes deployment paths."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml


ANSIBLE = Path(__file__).parent
PLAYBOOK = (ANSIBLE / "playbook.yml").read_text()
SERVICES = (ANSIBLE / "tasks" / "services.yml").read_text()
RUNTIME = (ANSIBLE / "tasks" / "runtime.yml").read_text()
STARTUP_NOTIFY = (ANSIBLE.parent / "ops" / "startup-notify.sh").read_text()
HEALTH_CHECK = (ANSIBLE.parent / "ops" / "health-check.sh").read_text()


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
        self.assertNotIn("--start-now", SERVICES)
        self.assertIn("meta: flush_handlers", SERVICES)
        self.assertIn("restart Hermes gateway", PLAYBOOK)
        self.assertIn("restart managed observability services", PLAYBOOK)

    def test_deployment_avoids_duplicate_gateway_alerts(self) -> None:
        for task_name in (
            "Materialize encrypted API keys and tokens for Hermes",
            "Apply the managed Hermes model and voice configuration",
            "Apply shared Hermes VPS runtime configuration",
        ):
            with self.subTest(task_name=task_name):
                task = RUNTIME.split(f"- name: {task_name}", 1)[1].split("\n- name:", 1)[0]
                self.assertIn("notify: restart Hermes gateway", task)
        self.assertNotIn("meta: flush_handlers", RUNTIME)
        self.assertIn("Mark planned Hermes gateway maintenance", SERVICES)
        self.assertIn("Clear planned Hermes gateway maintenance marker", PLAYBOOK)
        self.assertIn("gateway-maintenance", STARTUP_NOTIFY)
        self.assertIn("gateway-maintenance", HEALTH_CHECK)

    def test_maintenance_encloses_backup_install_and_final_service_checks(self) -> None:
        play = yaml.safe_load(PLAYBOOK)[0]
        deployment = next(task for task in play["tasks"]
                          if task.get("name") == "Deploy Hermes with bounded maintenance coverage")
        tasks = deployment["block"]
        names = [task.get("name") for task in tasks]
        marker = names.index("Mark maintenance before stopping an existing gateway")
        for name in ("Create and verify the mandatory config-only deployment backup",
                     "Install or update Hermes to the pinned commit",
                     "Stop the gateway before restoring a backup",
                     "Complete Hermes deployment and report its result"):
            self.assertLess(marker, names.index(name))
        cleanup = deployment["always"][-1]
        self.assertEqual(cleanup["name"], "Clear planned Hermes gateway maintenance marker")
        self.assertEqual(cleanup["ansible.builtin.file"]["state"], "absent")
        self.assertNotIn("Clear planned Hermes gateway maintenance marker", SERVICES)

    @unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible is required")
    def test_maintenance_cleanup_runs_on_success_and_failure(self) -> None:
        deployment = next(task for task in yaml.safe_load(PLAYBOOK)[0]["tasks"]
                          if task.get("name") == "Deploy Hermes with bounded maintenance coverage")
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "ops").mkdir()
                marker = root / "ops" / "gateway-maintenance"
                marker.touch()
                stage = ({"ansible.builtin.fail": {"msg": "simulated deployment failure"}}
                         if fail else {"ansible.builtin.debug": {"msg": "simulated success"}})
                play = [{"hosts": "localhost", "gather_facts": False,
                         "vars": {"hermes_home": str(root), "hermes_enable_ops": True},
                         "tasks": [{"block": [stage], "always": deployment["always"]}]}]
                path = root / "check.yml"
                path.write_text(yaml.safe_dump(play))
                result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                        capture_output=True, text=True, timeout=30,
                                        env={**os.environ, "ANSIBLE_CONFIG": str(ANSIBLE / "ansible.cfg")})
                self.assertEqual(result.returncode != 0, fail, result.stdout + result.stderr)
                self.assertFalse(marker.exists(), result.stdout + result.stderr)

    def test_pinned_external_engineering_skills_preserve_existing_directories(self) -> None:
        self.assertIn("Install pinned Matt Pocock engineering skills for Hermes", RUNTIME)
        self.assertIn("version: \"{{ vps_external_skills.matt_pocock_engineering.revision }}\"", RUNTIME)
        self.assertIn("Protect pinned Matt Pocock engineering skills from Hermes writes", RUNTIME)
        self.assertIn("Combine managed and existing external Hermes skill directories", RUNTIME)
        self.assertIn("hermes_existing_config.get('skills', {}).get('external_dirs', [])", RUNTIME)

    def test_external_skill_sources_are_not_world_readable(self) -> None:
        parent_task = RUNTIME.split(
            "- name: Create the root-owned external Hermes skills directory", 1
        )[1].split("- name: Install pinned Matt Pocock engineering skills for Hermes", 1)[0]
        protect_task = RUNTIME.split(
            "- name: Protect pinned Matt Pocock engineering skills from Hermes writes", 1
        )[1].split("- name: Verify required Matt Pocock engineering skill sources", 1)[0]
        self.assertIn('group: "{{ hermes_user }}"', parent_task)
        self.assertIn('mode: "0750"', parent_task)
        self.assertIn('group: "{{ hermes_user }}"', protect_task)
        self.assertIn('mode: "u=rwX,g=rX,o="', protect_task)

    def test_external_skill_checkout_is_confined_to_its_managed_prefix(self) -> None:
        self.assertIn(
            "vps_external_skills.matt_pocock_engineering.checkout_dir is match('^/opt/hermes-external-skills/",
            PLAYBOOK,
        )
        self.assertIn("'..' not in vps_external_skills.matt_pocock_engineering.checkout_dir.split('/')", PLAYBOOK)


if __name__ == "__main__":
    unittest.main()
