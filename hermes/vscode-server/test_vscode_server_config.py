"""Regression tests for the operator-only code-server deployment."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path

import yaml


VSCODE_DIR = Path(__file__).parent
HERMES_DIR = VSCODE_DIR.parent


class VscodeServerConfigTests(unittest.TestCase):
    def test_compose_is_ssh_only_and_mounts_managed_git_credentials(self) -> None:
        compose = yaml.safe_load(
            (VSCODE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        )
        self.assertIn("CODE_SERVER_PROJECT_NAME", compose["name"])
        service = compose["services"]["code-server"]

        self.assertIn("CODE_SERVER_BIND_ADDRESS", service["ports"][0])
        self.assertIn("CODE_SERVER_HOST_PORT", service["ports"][0])
        settings = yaml.safe_load(
            (HERMES_DIR / "config" / "vps-defaults.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(settings["vps_vscode"]["bind_address"], "127.0.0.1")
        self.assertEqual(settings["vps_vscode"]["host_port"], 3001)
        self.assertIn("@sha256:", settings["vps_vscode"]["image"])
        self.assertEqual(
            service["build"]["args"],
            {
                "CODE_SERVER_IMAGE": "${CODE_SERVER_IMAGE:?set CODE_SERVER_IMAGE in /etc/code-server.env}",
                "HERMES_UID": "${HERMES_UID:?set HERMES_UID in /etc/code-server.env}",
                "HERMES_GID": "${HERMES_GID:?set HERMES_GID in /etc/code-server.env}",
            },
        )

        volumes = service["volumes"]
        self.assertTrue(any("CODE_SERVER_REPOSITORIES_DIR" in item for item in volumes))
        self.assertTrue(any("VPS_HERMES_HOME" in item for item in volumes))
        self.assertIn(
            "../runtime/github-cli-wrapper.py:/home/coder/.local/bin/gh:ro",
            volumes,
        )
        self.assertFalse(any(".config/gh" in item for item in volumes))

        environment = service["environment"]
        self.assertEqual(environment["HERMES_HOME"], "/home/coder/hermes-home")
        self.assertTrue(environment["PATH"].startswith("/home/coder/.local/bin:"))
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_1"],
            "!/home/coder/.local/bin/gh auth git-credential",
        )

    def test_strong_password_is_json_escaped_before_dotenv_rendering(self) -> None:
        tasks = (HERMES_DIR / "ansible" / "tasks" / "vscode.yml").read_text(
            encoding="utf-8"
        )
        template = (
            HERMES_DIR / "ansible" / "templates" / "code-server.env.j2"
        ).read_text(encoding="utf-8")
        self.assertIn("hermes_code_server_password | length >= 16", tasks)
        self.assertIn("hermes_code_server_password | length <= 128", tasks)
        self.assertIn("hermes_code_server_password != 'replace-inside-ansible-vault'", tasks)
        self.assertIn(
            "hermes_code_server_password is match('^[A-Za-z0-9._~!@%^+=:,-]{16,128}$')",
            tasks,
        )
        self.assertIn("PASSWORD={{ hermes_code_server_password | to_json }}", template)
        self.assertIn("VPS_USER_HOME={{ hermes_user_home }}", template)
        self.assertIn("VPS_HERMES_HOME={{ hermes_home }}", template)
        self.assertIn("HERMES_UID={{ hermes_vscode_uid.stdout }}", template)
        self.assertIn("HERMES_GID={{ hermes_vscode_gid.stdout }}", template)
        self.assertIn("CODE_SERVER_REPOSITORIES_DIR={{ hermes_github_repositories_dir }}", template)
        self.assertIn("CODE_SERVER_IMAGE={{ vps_vscode.image }}", template)
        self.assertIn("CODE_SERVER_HOST_PORT={{ vps_vscode.host_port }}", template)

    def test_host_mounts_are_prepared_and_container_identity_is_verified(self) -> None:
        tasks = (HERMES_DIR / "ansible" / "tasks" / "vscode.yml").read_text(
            encoding="utf-8"
        )
        task_definitions = yaml.safe_load(tasks)
        services = (HERMES_DIR / "ansible" / "tasks" / "services.yml").read_text(
            encoding="utf-8"
        )
        dockerfile = (VSCODE_DIR / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn('path: "{{ hermes_github_repositories_dir }}"', tasks)
        self.assertIn('path: "{{ hermes_user_home }}/.gitconfig"', tasks)
        self.assertIn('VPS_USER_HOME: "{{ hermes_user_home }}"', tasks)
        self.assertIn("Detect the legacy code-server container", tasks)
        self.assertIn("Stop the legacy code-server container without deleting its data", tasks)
        self.assertIn("Preserve the legacy code-server container for manual data migration", tasks)
        self.assertIn("vscode-server-code-server-1", tasks)
        self.assertNotIn("docker\n      - container\n      - rm", tasks)
        self.assertIn("vscode-server-code-server-1-legacy-", tasks)
        self.assertIn("Verify the managed GitHub account inside code-server", tasks)
        self.assertIn("'--vscode-enabled'", services)
        self.assertIn("vps_deploy.features.vscode_server", services)
        start_task = next(
            task
            for task in task_definitions
            if task["name"] == "Start the managed code-server container"
        )
        self.assertEqual(
            start_task["environment"],
            {
                "VPS_USER_HOME": "{{ hermes_user_home }}",
                "HERMES_UID": "{{ hermes_vscode_uid.stdout }}",
                "HERMES_GID": "{{ hermes_vscode_gid.stdout }}",
            },
        )
        self.assertNotIn("environment", start_task["ansible.builtin.command"])
        self.assertIn("ARG HERMES_UID=1000", dockerfile)
        self.assertIn("ARG HERMES_GID=1000", dockerfile)
        self.assertIn("ARG CODE_SERVER_IMAGE", dockerfile)
        self.assertNotIn("code-server:latest", dockerfile)
        self.assertIn('usermod -g "${HERMES_GID}" coder', dockerfile)

    def test_managed_wrapper_is_executable_and_manual_password_setup_is_safe(self) -> None:
        wrapper = HERMES_DIR / "runtime" / "github-cli-wrapper.py"
        readme = (VSCODE_DIR / "README.md").read_text(encoding="utf-8")

        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        self.assertIn("read -rsp", readme)
        self.assertIn("Set a unique code-server password of 16-128 characters.", readme)
        self.assertIn("CODE_SERVER_PASSWORD_JSON", readme)
        self.assertIn("sudo install -o root -g root -m 0600", readme)
        self.assertNotIn('echo "PASSWORD=', readme)


if __name__ == "__main__":
    unittest.main()
