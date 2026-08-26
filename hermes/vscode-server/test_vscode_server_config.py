"""Regression tests for the operator-only code-server deployment."""

from __future__ import annotations

import re
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
        self.assertEqual(compose["name"], "hermes-vscode")
        service = compose["services"]["code-server"]

        self.assertEqual(service["ports"], ["127.0.0.1:3001:8080"])
        self.assertEqual(
            service["build"]["args"],
            {
                "HERMES_UID": "${HERMES_UID:?set HERMES_UID in /etc/code-server.env}",
                "HERMES_GID": "${HERMES_GID:?set HERMES_GID in /etc/code-server.env}",
            },
        )

        volumes = service["volumes"]
        self.assertTrue(
            any("workspace/repositories:/home/coder/workspace/repositories" in item for item in volumes)
        )
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

    def test_password_is_validated_before_plain_dotenv_rendering(self) -> None:
        tasks = (HERMES_DIR / "ansible" / "tasks" / "vscode.yml").read_text(
            encoding="utf-8"
        )
        template = (
            HERMES_DIR / "ansible" / "templates" / "code-server.env.j2"
        ).read_text(encoding="utf-8")
        match_line = next(
            line for line in tasks.splitlines() if "hermes_code_server_password is match(" in line
        )
        pattern = match_line.split("match('", 1)[1].rsplit("')", 1)[0]

        self.assertIsNotNone(re.fullmatch(pattern, "Strong-Manual_Review:2026"))
        self.assertIsNone(re.fullmatch(pattern, "password'breaks-dotenv"))
        self.assertIn("replace-inside-ansible-vault", tasks)
        self.assertIn("PASSWORD={{ hermes_code_server_password }}", template)
        self.assertNotIn("| quote", template)
        self.assertIn("VPS_USER_HOME={{ hermes_user_home }}", template)
        self.assertIn("HERMES_UID={{ hermes_vscode_uid.stdout }}", template)
        self.assertIn("HERMES_GID={{ hermes_vscode_gid.stdout }}", template)

    def test_host_mounts_are_prepared_and_container_identity_is_verified(self) -> None:
        tasks = (HERMES_DIR / "ansible" / "tasks" / "vscode.yml").read_text(
            encoding="utf-8"
        )
        services = (HERMES_DIR / "ansible" / "tasks" / "services.yml").read_text(
            encoding="utf-8"
        )
        dockerfile = (VSCODE_DIR / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn('path: "{{ hermes_user_home }}/workspace/repositories"', tasks)
        self.assertIn('path: "{{ hermes_user_home }}/.gitconfig"', tasks)
        self.assertIn('VPS_USER_HOME: "{{ hermes_user_home }}"', tasks)
        self.assertIn("Verify the managed GitHub account inside code-server", tasks)
        self.assertIn("'--vscode-enabled'", services)
        self.assertIn("vps_deploy.features.vscode_server", services)
        self.assertIn("ARG HERMES_UID=1000", dockerfile)
        self.assertIn("ARG HERMES_GID=1000", dockerfile)
        self.assertIn('usermod -g "${HERMES_GID}" coder', dockerfile)

    def test_managed_wrapper_is_executable_and_manual_password_setup_is_safe(self) -> None:
        wrapper = HERMES_DIR / "runtime" / "github-cli-wrapper.py"
        readme = (VSCODE_DIR / "README.md").read_text(encoding="utf-8")

        self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        self.assertIn("read -rsp", readme)
        self.assertIn("sudo install -o root -g root -m 0600", readme)
        self.assertNotIn('echo "PASSWORD=', readme)


if __name__ == "__main__":
    unittest.main()
