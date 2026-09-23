"""Local shell/Git and Ansible-policy regressions for interrupted installs."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from jinja2 import Environment
import yaml


ROOT = Path(__file__).resolve().parents[1]


class InstallRecoveryTests(unittest.TestCase):
    def run_shell(self, home, body):
        script = f"""
set -Eeuo pipefail
source {shlex.quote(str(ROOT / 'deploy/runtime.sh'))}
HERMES_USER_HOME={shlex.quote(str(home))}
SCRIPT_DIR={shlex.quote(str(ROOT))}
VPS_CONFIG_APPLIER="$SCRIPT_DIR/runtime/apply-config.py"
VPS_SETTINGS_FILE="$SCRIPT_DIR/config/vps-defaults.yml"
INSTALL_DEV_CLIS=true
HERMES_USER=test HERMES_GROUP=test
install() {{ :; }}
log() {{ :; }}
warn() {{ :; }}
die() {{ exit 1; }}
run_as_hermes() {{
  if [[ "$1 ${{2:-}}" == 'git lfs' ]]; then return; fi
  "$@"
}}
{body}
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=30, env={**os.environ, "HOME": str(home),
                                              "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                                              "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
                                              "GIT_CONFIG_NOSYSTEM": "1"})

    def test_git_defaults_repair_duplicates_and_are_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".gitconfig"
            config.write_text('[init]\n defaultBranch = old\n defaultBranch = older\n'
                              '[alias]\n personal = status --short\n')
            for _ in range(2):
                result = self.run_shell(home, "configure_development_clis")
                self.assertEqual(result.returncode, 0, result.stderr)
            expected = yaml.safe_load((ROOT / "config/vps-defaults.yml").read_text())["vps_github"]["git_defaults"]
            actual = subprocess.check_output(["git", "config", "--file", str(config),
                                              "--get-all", "init.defaultBranch"], text=True)
            self.assertEqual(actual.splitlines(), [expected["default_branch"]])
            self.assertIn("personal = status --short", config.read_text())

    def test_ansible_owned_git_block_is_not_rewritten_by_installer(self):
        tasks = yaml.safe_load((ROOT / "ansible/tasks/github.yml").read_text())
        task = next(t for t in tasks if t.get("name") == "Configure managed Git defaults and commit identity")
        defaults = yaml.safe_load((ROOT / "config/vps-defaults.yml").read_text())
        env = Environment()
        env.filters["ternary"] = lambda value, yes, no: yes if value else no
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            block = env.from_string(task["ansible.builtin.blockinfile"]["block"]).render(
                **defaults, hermes_git_identity={"name": "Test", "email": "test@example.org"},
                hermes_github_host="github.com", hermes_user_home=str(home))
            content = ('[init]\n defaultBranch = old\n[alias]\n personal = status\n'
                       '# BEGIN HERMES MANAGED GIT DEFAULTS\n' + block +
                       '\n# END HERMES MANAGED GIT DEFAULTS\n')
            (home / ".gitconfig").write_text(content)
            for _ in range(2):
                result = self.run_shell(home, "configure_development_clis")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((home / ".gitconfig").read_text(), content)

    def test_same_commit_without_completed_install_still_requires_full_install(self):
        tasks = yaml.safe_load((ROOT / "ansible/playbook.yml").read_text())[0]["tasks"]
        task = next(t for t in tasks if t.get("name") == "Decide whether the pinned Hermes source needs installation")
        expression = task["ansible.builtin.set_fact"]["hermes_source_update_required"]
        for completed, binary, python, head, expected in (
            ("", True, True, "pinned", True), ("old", True, True, "pinned", True),
            ("pinned", True, True, "pinned", False), ("pinned", False, True, "pinned", True),
            ("pinned", True, False, "pinned", True), ("pinned", True, True, "old", True),
        ):
            with self.subTest(completed=completed, binary=binary, python=python, head=head):
                result = Environment().from_string(expression).render(
                    hermes_binary={"stat": {"exists": binary}},
                    hermes_venv_python={"stat": {"exists": python}},
                    hermes_installed_commit={"stdout": head}, hermes_commit="pinned",
                    hermes_completed_install={"stdout": completed})
                self.assertEqual(result.strip(), str(expected))

    def test_completion_marker_is_invalidated_before_mutation_and_written_only_on_success(self):
        deploy = (ROOT / "deploy-hermes.sh").read_text()
        main = "main() {" + deploy.split("\nmain() {", 1)[1].rsplit("\nmain\n", 1)[0]
        # Run real main + install_hermes, replacing host operations and upstream
        # execution. Exercise an old success marker followed by a failed retry.
        stubs = "\n".join(f"{name}() {{ :; }}" for name in (
            "resolve_source_pin", "validate_inputs", "install_host_dependencies", "install_tailscale",
            "ensure_service_user", "resolve_user_paths", "resolve_managed_runtime", "enable_host_administration",
            "download_installer", "quiesce_existing_gateway_for_update", "backup_existing_installation",
            "verify_updated_kanban_state", "apply_local_hermes_patches", "install_local_browser_automation",
            "configure_development_clis", "install_google_workspace_cli", "apply_recommended_defaults",
            "initialize_skills_hub", "resolve_gateway_choice", "install_operations_layer",
            "restart_managed_runtime", "run_tailscale_login", "run_diagnostics", "print_summary"))
        for fail_at in ("none", "installer", "configure_development_clis", "backup_existing_installation"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                marker = home / ".hermes-install-complete"
                marker.write_text("old\n")
                body = f"""
{stubs}
{main}
HERMES_COMMIT=pinned HERMES_VERSION=test HERMES_BRANCH=main HERMES_RELEASE=test
HERMES_USER=test HERMES_HOME="$HERMES_USER_HOME/state" HERMES_INSTALL_DIR="$HERMES_HOME/hermes-agent"
HERMES_BIN=/bin/bash INSTALLER_FILE=/fixture/installer WITH_BROWSER=false
UPDATE_GUARD_ACTIVE=true RUN_SETUP=false RUN_MCP_PICKER=false ENABLE_GATEWAY=false
run_as_hermes() {{
  if [[ "$1" == bash && "$2" == "$INSTALLER_FILE" ]]; then
    [[ ! -s "$HERMES_USER_HOME/.hermes-install-complete" ]] || exit 91
    [[ {shlex.quote(fail_at)} != installer ]] || exit 92
  elif [[ "$1" == git ]]; then printf 'pinned\\n';
  elif [[ "$1" == */venv/bin/python ]]; then printf 'test\\n';
  else "$@"; fi
}}
"""
                if fail_at == "configure_development_clis":
                    body += "configure_development_clis() { exit 93; }\n"
                if fail_at == "backup_existing_installation":
                    body += "backup_existing_installation() { exit 94; }\n"
                result = self.run_shell(home, body + "main\n")
                if fail_at == "none":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(marker.read_text().strip(), "pinned")
                elif fail_at == "backup_existing_installation":
                    self.assertEqual(result.returncode, 94, result.stderr)
                    self.assertEqual(marker.read_text(), "old\n")
                else:
                    self.assertEqual(result.returncode, 92 if fail_at == "installer" else 93, result.stderr)
                    self.assertFalse(marker.exists() and marker.read_text().strip() == "old")

    def test_completion_marker_write_failure_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".hermes-install-complete").mkdir()
            result = self.run_shell(home, "set +e\nHERMES_COMMIT=pinned\nrecord_installation_completion")
            self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(shutil.which("ansible-playbook"), "Ansible is required")
    def test_completion_marker_tasks_with_real_ansible(self):
        tasks = yaml.safe_load((ROOT / "ansible/playbook.yml").read_text())[0]["tasks"]
        read = next(t for t in tasks if t.get("name") == "Read the last completed Hermes installation commit")
        decide = next(t for t in tasks if t.get("name") == "Decide whether the pinned Hermes source needs installation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checks = []
            for label, contents in (("missing", None), ("empty", ""), ("old", "old\n"), ("complete", "pinned\n")):
                home = root / label
                home.mkdir()
                if contents is not None:
                    (home / ".hermes-install-complete").write_text(contents)
                checks.append({"vars": {"hermes_user_home": str(home)}, "block": [
                    read, decide, {"ansible.builtin.assert": {"that": [
                        "not hermes_source_update_required" if label == "complete" else "hermes_source_update_required"
                    ]}},
                ]})
            play = [{"hosts": "localhost", "gather_facts": False, "vars": {
                "ansible_python_interpreter": sys.executable,
                "hermes_binary": {"stat": {"exists": True}},
                "hermes_venv_python": {"stat": {"exists": True}},
                "hermes_installed_commit": {"stdout": "pinned"}, "hermes_commit": "pinned",
            }, "tasks": checks}]
            path = root / "check.yml"
            path.write_text(yaml.safe_dump(play))
            result = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
                                    capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "ANSIBLE_CONFIG": str(ROOT / "ansible/ansible.cfg")})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
