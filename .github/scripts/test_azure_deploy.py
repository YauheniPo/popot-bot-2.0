"""Deployment selection is resolved before approval, without executing target code."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / "azure-ci/scripts/prepare-hermes-deploy.py"


class DeploymentSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.repo = Path(self.temp) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.file = self.repo / "hermes/ansible/playbook.yml"
        self.file.parent.mkdir(parents=True)
        self.file.write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.git("update-ref", "refs/remotes/origin/feature/deploy", self.sha)
        self.output = Path(self.temp) / "artifact"

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)

    def prepare(self, branch="refs/heads/feature/deploy", mode="full", commit=None):
        return subprocess.run(
            [sys.executable, str(PREPARE)], cwd=self.repo, text=True,
            capture_output=True, check=False,
            env={**os.environ, "DEPLOY_BRANCH": branch, "DEPLOY_MODE": mode,
                 "DEPLOY_COMMIT": self.sha if commit is None else commit,
                 "DEPLOY_ARTIFACT_DIR": str(self.output)},
        )

    def test_snapshot_stays_pinned_when_branch_moves_and_excludes_local_files(self):
        (self.repo / "untracked-secret").write_text("must not be archived")
        self.file.write_text("dirty local content\n")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git("add", "hermes")
        self.git("commit", "-qm", "branch moved")
        self.git("update-ref", "refs/remotes/origin/feature/deploy", "HEAD")
        with tarfile.open(self.output / "source.tar") as archive:
            self.assertEqual(archive.extractfile("hermes/ansible/playbook.yml").read(), b"original\n")
            self.assertNotIn(".git", archive.getnames())
            self.assertNotIn("untracked-secret", archive.getnames())
        manifest = json.loads((self.output / "deployment.json").read_text())
        self.assertEqual(manifest, {"branch": "feature/deploy", "commit": self.sha, "mode": "full"})
        self.assertIn(self.sha, (self.output / "deployment.md").read_text())
        self.assertIn("task.uploadsummary", result.stdout)

    def test_modes_and_fully_qualified_branch(self):
        for mode in ("full", "config-only", "runtime-only"):
            with self.subTest(mode=mode):
                result = self.prepare("refs/heads/feature/deploy", mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads((self.output / "deployment.json").read_text())
                self.assertEqual(manifest["mode"], mode)
                self.assertEqual(manifest["branch"], "feature/deploy")

    def test_invalid_branch_metadata_fails_before_archive(self):
        for branch in ("", "-option", "refs/heads/feature/../deploy", "HEAD",
                       "refs/tags/v1", "$(touch injected)", "refs/heads/main\n##vso[bad]",
                       "refs/heads/feature/*", "refs/heads/", "refs/heads/refs/other"):
            with self.subTest(branch=branch):
                result = self.prepare(branch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR:", result.stderr)
                self.assertFalse((self.output / "source.tar").exists())

    def test_selected_resource_commit_is_not_re_resolved_from_moving_branch(self):
        self.file.write_text("new branch tip\n")
        self.git("add", ".")
        self.git("commit", "-qm", "advance source branch before preparation")
        self.git("update-ref", "refs/remotes/origin/feature/deploy", "HEAD")
        self.git("checkout", "--detach", self.sha)
        for _ in range(2):
            result = self.prepare()
            self.assertEqual(result.returncode, 0, result.stderr)
            with tarfile.open(self.output / "source.tar") as archive:
                self.assertEqual(archive.extractfile("hermes/ansible/playbook.yml").read(), b"original\n")
            manifest = json.loads((self.output / "deployment.json").read_text())
            self.assertEqual(manifest["commit"], self.sha)

    def test_missing_invalid_or_mismatched_resource_commit_fails_before_archive(self):
        for commit in ("", "HEAD", "--help", "$(deploymentSourceVersion)",
                       "a" * 40, self.sha[:12], self.sha + "\n##vso[bad]"):
            with self.subTest(commit=commit):
                result = self.prepare(commit=commit)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("commit", result.stderr.lower())
                self.assertFalse((self.output / "source.tar").exists())

    def test_source_helper_is_never_executed(self):
        helper = self.repo / "azure-ci/scripts/prepare-hermes-deploy.py"
        helper.parent.mkdir(parents=True)
        marker = self.repo / "executed-untrusted-code"
        helper.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.git("add", ".")
        self.git("commit", "-qm", "untrusted helper fixture")
        result = self.prepare(commit=self.git("rev-parse", "HEAD").strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_invalid_mode_fails_before_archive(self):
        result = self.prepare(mode="full -e unsafe=true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mode", result.stderr)
        self.assertFalse((self.output / "source.tar").exists())

    def test_branch_without_playbook_fails_before_archive(self):
        self.git("rm", "hermes/ansible/playbook.yml")
        self.git("commit", "-qm", "remove playbook")
        self.git("update-ref", "refs/remotes/origin/feature/deploy", "HEAD")
        result = self.prepare(commit=self.git("rev-parse", "HEAD").strip())
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.output / "source.tar").exists())


class DeploymentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = yaml.safe_load((ROOT / "azure-ci/azure-deploy-hermes.yml").read_text())

    def test_vault_examples_do_not_require_a_vps_join_key_for_redeploy(self):
        for relative_path in ("azure-secure-files/vault.yml.example", "group_vars/all/vault.yml.example"):
            with self.subTest(example=relative_path):
                example = yaml.safe_load((ROOT / "hermes/ansible" / relative_path).read_text())
                self.assertNotIn("tailscale_auth_key", example)
                self.assertNotIn("GITHUB_TOKEN", example)
        ado_example = yaml.safe_load((ROOT / "hermes/ansible/azure-secure-files/vault.yml.example").read_text())
        self.assertIn("GITHUB_TOKEN", ado_example["hermes_secret_env"])

    def deployment_steps(self):
        return self.pipeline["stages"][1]["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]

    def test_manual_run_needs_no_confirmation_parameter_and_keeps_protected_stage(self):
        self.assertEqual(self.pipeline["trigger"], "none")
        self.assertEqual(self.pipeline["pr"], "none")
        self.assertCountEqual([p["name"] for p in self.pipeline["parameters"]],
                              ["deployBranch", "deployMode"])
        content = json.dumps(self.pipeline)
        for obsolete in ("confirmProduction", "CONFIRM_PRODUCTION", "Validate production confirmation"):
            self.assertNotIn(obsolete, content)
        validate, deploy = self.pipeline["stages"]
        self.assertEqual(validate["jobs"][0]["steps"][0]["template"],
                         "azure-templates/validate-trusted-branch.yml")
        self.assertEqual(deploy["dependsOn"], validate["stage"])
        self.assertEqual(deploy["condition"], "succeeded()")
        self.assertEqual(deploy["lockBehavior"], "sequential")
        self.assertEqual(deploy["jobs"][0]["environment"], "hermes-vps")

    def test_agent_temp_is_not_used_as_remote_vps_temp(self):
        steps = [step for step in self.deployment_steps()
                 if "ANSIBLE_LOCAL_TEMP" in step.get("env", {})]
        self.assertEqual(len(steps), 3)
        for step in steps:
            self.assertNotIn("ANSIBLE_REMOTE_TEMP", step["env"])
            self.assertNotIn("ANSIBLE_REMOTE_TEMP", step["bash"])

    def test_tailnet_join_is_protected_and_cleanup_runs_on_failure_or_cancel(self):
        steps = self.deployment_steps()
        files = [s["inputs"]["secureFile"] for s in steps if s.get("task") == "DownloadSecureFile@1"]
        self.assertCountEqual(files, ["vault.yml", "hermes-vps-known-hosts"])
        self.assertEqual(self.pipeline["stages"][1]["variables"], [{"group": "hermes-deploy-secrets"}])
        self.assertNotIn("variables", self.pipeline["stages"][0])
        self.assertNotIn("variables", self.pipeline)
        join = next(s for s in steps if s.get("name") == "JoinTailnet")
        probe = next(s for s in steps if s.get("name") == "ProbeDeploySsh")
        cleanup = next(s for s in steps if s.get("name") == "LeaveTailnet")
        deploy = next(s for s in steps if s.get("displayName") == "Deploy Hermes with Ansible")
        self.assertLess(steps.index(join), steps.index(probe))
        self.assertLess(steps.index(probe), steps.index(deploy))
        self.assertGreater(steps.index(cleanup), steps.index(deploy))
        self.assertEqual(cleanup["condition"], "always()")
        self.assertIn("tailscale logout", cleanup["bash"])
        for step in (probe, deploy):
            options = step["env"]["ANSIBLE_SSH_COMMON_ARGS"]
            self.assertIn("BatchMode=yes", options)
            self.assertIn("StrictHostKeyChecking=yes", options)
            self.assertIn("ConnectTimeout=15", options)
            for option in ("PubkeyAuthentication=no", "PasswordAuthentication=no",
                           "KbdInteractiveAuthentication=no", "IdentityAgent=none"):
                self.assertIn(option, options)
        self.assertIn("timeout --kill-after=5s 75s", probe["bash"])

    def test_pipeline_has_no_private_key_dependency(self):
        content = (ROOT / "azure-ci/azure-deploy-hermes.yml").read_text()
        for obsolete in ("hermes-vps-ssh-key", "hermesSshKey", "HERMES_SSH_KEY_FILE", "--private-key"):
            self.assertNotIn(obsolete, content)

    def test_input_validation_needs_only_vault_known_hosts_and_secret_variables(self):
        step = next(s for s in self.deployment_steps() if s.get("displayName") == "Validate protected deployment inputs")
        with tempfile.TemporaryDirectory() as temp:
            inputs = {
                "HERMES_VAULT_FILE": "$ANSIBLE_VAULT;1.1;AES256\ntest-encrypted-fixture\n",
                "HERMES_KNOWN_HOSTS_FILE": "test-host ssh-ed25519 test-public-fixture\n",
                "HERMES_VAULT_PASSWORD_FILE": "test-password\n",
                "HERMES_TAILSCALE_KEY_FILE": "tskey-auth-TESTKEY\n",
            }
            env = dict(os.environ)
            env.pop("HERMES_SSH_KEY_FILE", None)
            for name, content in inputs.items():
                path = Path(temp) / name
                path.write_text(content)
                env[name] = str(path)
            result = subprocess.run(["bash", "-c", step["bash"]], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in inputs:
                self.assertEqual(Path(env[name]).stat().st_mode & 0o777, 0o600)
            self.assertNotIn("test-password", result.stdout + result.stderr)
            Path(env["HERMES_KNOWN_HOSTS_FILE"]).unlink()
            result = subprocess.run(["bash", "-c", step["bash"]], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("required deployment input file", result.stdout)

    def run_secret_step(self, name, temp, **values):
        step = next(s for s in self.deployment_steps() if s.get("name") == name)
        return subprocess.run(
            ["bash", "-c", step["bash"]], text=True, capture_output=True, check=False,
            env={**os.environ, "AGENT_TEMPDIRECTORY": temp,
                 "HERMES_VAULT_PASSWORD": "test-password", "HERMES_TAILSCALE_AUTH_KEY": "tskey-auth-TESTKEY", **values},
        )

    def test_secret_variables_are_materialized_privately_and_cleaned_without_logs(self):
        with tempfile.TemporaryDirectory() as temp:
            password = 'pass $HOME `id` "quotes" \\ кириллица'
            result = self.run_secret_step("PrepareDeploySecrets", temp, HERMES_VAULT_PASSWORD=password)
            self.assertEqual(result.returncode, 0, result.stderr)
            directory = Path(temp) / "hermes-deploy-secrets"
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for name, value in (("vault-password", password), ("tailscale-auth-key", "tskey-auth-TESTKEY")):
                path = directory / name
                self.assertEqual(path.read_text(), value + "\n")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertNotIn(value, result.stdout + result.stderr)
            untouched = Path(temp) / "unrelated"
            untouched.write_text("preserve")
            for _ in range(2):
                cleanup = self.run_secret_step("CleanDeploySecrets", temp)
                self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
                self.assertFalse(directory.exists())
                self.assertEqual(untouched.read_text(), "preserve")

    def test_secret_validation_rejects_missing_macros_and_multiline_without_writes(self):
        invalid = [
            {"HERMES_VAULT_PASSWORD": ""},
            {"HERMES_VAULT_PASSWORD": "$(HERMES_VAULT_PASSWORD)"},
            {"HERMES_VAULT_PASSWORD": "line1\nline2"},
            {"HERMES_TAILSCALE_AUTH_KEY": "$(HERMES_TAILSCALE_AUTH_KEY)"},
            {"HERMES_TAILSCALE_AUTH_KEY": "not-an-auth-key"},
        ]
        for values in invalid:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temp:
                result = self.run_secret_step("PrepareDeploySecrets", temp, **values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("hermes-deploy-secrets", result.stdout + result.stderr)
                self.assertFalse((Path(temp) / "hermes-deploy-secrets").exists())

    def test_secret_preparation_refuses_existing_directory_and_cleans_partial_state(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "hermes-deploy-secrets"
            directory.mkdir(mode=0o700)
            password = directory / "vault-password"
            password.write_text("existing-secret\n")
            result = self.run_secret_step("PrepareDeploySecrets", temp)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot create private deployment secret files", result.stderr)
            self.assertNotIn("existing-secret", result.stdout + result.stderr)
            self.assertEqual(password.read_text(), "existing-secret\n")
            self.assertFalse((directory / "tailscale-auth-key").exists())
            cleanup = self.run_secret_step("CleanDeploySecrets", temp)
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertFalse(directory.exists())

    def test_only_preparation_receives_raw_secrets_and_cleanup_is_unconditional(self):
        steps = self.deployment_steps()
        prepare = next(s for s in steps if s.get("name") == "PrepareDeploySecrets")
        cleanup = next(s for s in steps if s.get("name") == "CleanDeploySecrets")
        self.assertEqual(cleanup["condition"], "always()")
        self.assertLess(steps.index(prepare), steps.index(next(s for s in steps if s.get("name") == "JoinTailnet")))
        for variable in ("HERMES_VAULT_PASSWORD", "HERMES_TAILSCALE_AUTH_KEY"):
            self.assertEqual(prepare["env"][variable], f"$({variable})")
            self.assertEqual(sum(variable in s.get("env", {}) for s in steps), 1)
        for step in steps:
            env = step.get("env", {})
            if "HERMES_VAULT_PASSWORD_FILE" in env:
                self.assertEqual(env["HERMES_VAULT_PASSWORD_FILE"], "$(Agent.TempDirectory)/hermes-deploy-secrets/vault-password")
            if "HERMES_TAILSCALE_KEY_FILE" in env:
                self.assertEqual(env["HERMES_TAILSCALE_KEY_FILE"], "$(Agent.TempDirectory)/hermes-deploy-secrets/tailscale-auth-key")

    def test_join_passes_key_as_file_and_reports_failure_without_leaking_secret(self):
        join = next(s for s in self.deployment_steps() if s.get("name") == "JoinTailnet")
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "key"
            key.write_text("tskey-auth-TESTSECRET\n")
            for rc in (0, 124, 1):
                result = subprocess.run(
                    ["bash", "-c", 'sudo() { printf "%s\\n" "$@"; return "$MOCK_RC"; }\n' + join["bash"]],
                    capture_output=True, text=True, check=False,
                    env={**os.environ, "HERMES_TAILSCALE_KEY_FILE": str(key),
                         "TAILSCALE_HOSTNAME": "ado-hermes-test", "MOCK_RC": str(rc)},
                )
                self.assertEqual(result.returncode == 0, rc == 0, result.stderr)
                self.assertIn(f"--auth-key=file:{key}", result.stdout)
                self.assertIn("--advertise-tags=tag:hermes-deploy", result.stdout)
                self.assertIn("--ssh=false", result.stdout)
                self.assertNotIn("TESTSECRET", result.stdout + result.stderr)
                if rc:
                    self.assertIn("task.logissue type=error", result.stdout)

    def test_join_rejects_empty_key_without_invoking_sudo(self):
        join = next(s for s in self.deployment_steps() if s.get("name") == "JoinTailnet")
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "key"
            key.touch()
            result = subprocess.run(
                ["bash", "-c", 'sudo() { echo UNEXPECTED_SUDO; }\n' + join["bash"]],
                capture_output=True, text=True, check=False,
                env={**os.environ, "HERMES_TAILSCALE_KEY_FILE": str(key),
                     "TAILSCALE_HOSTNAME": "ado-hermes-test"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("UNEXPECTED_SUDO", result.stdout)

    def test_ssh_preflight_fails_closed_on_auth_failure_and_timeout(self):
        probe = next(s for s in self.deployment_steps() if s.get("name") == "ProbeDeploySsh")
        with tempfile.TemporaryDirectory() as temp:
            for rc in (0, 124, 255):
                result = subprocess.run(
                    ["bash", "-c", 'timeout() { printf "%s\\n" "$@"; return "$MOCK_RC"; }\n' + probe["bash"]],
                    capture_output=True, text=True, check=False,
                    env={**os.environ, "MOCK_RC": str(rc), "ANSIBLE_LOCAL_TEMP": temp,
                         "ANSIBLE_REMOTE_TEMP": temp, "HERMES_VAULT_FILE": "vault path",
                         "HERMES_VAULT_PASSWORD_FILE": "password path"},
                )
                self.assertEqual(result.returncode == 0, rc == 0)
                self.assertIn("75s\nansible\nhermes_vps", result.stdout)
                self.assertIn("ansible.builtin.raw", result.stdout)
                self.assertIn("--become", result.stdout)
                self.assertNotIn("--private-key", result.stdout)
                if rc:
                    self.assertIn("Deploy was not started", result.stdout)

    def test_cleanup_stops_daemon_even_when_logout_fails(self):
        cleanup = next(s for s in self.deployment_steps() if s.get("name") == "LeaveTailnet")
        result = subprocess.run(
            ["bash", "-c", '''tailscale() { :; }
sudo() { printf '%s\\n' "$@"; [[ "$*" != *'tailscale logout'* ]]; }
''' + cleanup["bash"]], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("type=warning", result.stdout)
        self.assertIn("systemctl\nstop\ntailscaled", result.stdout)

    def test_trusted_checkout_and_snapshot_precede_protected_stage(self):
        validate, deploy = self.pipeline["stages"]
        steps = validate["jobs"][0]["steps"]
        checkout = next(s for s in steps if s.get("checkout") == "self")
        self.assertEqual(checkout["fetchDepth"], 0)
        self.assertIs(checkout["persistCredentials"], False)
        self.assertEqual(checkout["path"], "s/pipeline")
        source_checkout = next(s for s in steps if s.get("checkout") == "deploySource")
        self.assertEqual(source_checkout["path"], "s/deploy-source")
        self.assertIs(source_checkout["persistCredentials"], False)
        prepare = next(s for s in steps if "prepare-hermes-deploy.py" in s.get("bash", ""))
        self.assertEqual(prepare["bash"],
                         'python3 "$(Pipeline.Workspace)/s/pipeline/azure-ci/scripts/prepare-hermes-deploy.py"')
        self.assertEqual(prepare["workingDirectory"], "$(Pipeline.Workspace)/s/deploy-source")
        self.assertEqual(prepare["env"]["DEPLOY_BRANCH"], "$(deploymentSourceRef)")
        self.assertEqual(prepare["env"]["DEPLOY_COMMIT"], "$(deploymentSourceVersion)")
        self.assertLess(steps.index(source_checkout), steps.index(prepare))
        self.assertTrue(any("validate-trusted-branch" in s.get("template", "") for s in steps))
        publish = next(s for s in steps if s.get("task") == "PublishPipelineArtifact@1")
        self.assertEqual(publish["inputs"]["artifact"], "hermes-deploy-source")
        self.assertEqual(deploy["dependsOn"], validate["stage"])
        self.assertEqual(deploy["jobs"][0]["environment"], "hermes-vps")
        deploy_steps = deploy["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]
        self.assertFalse(any(s.get("checkout") == "self" for s in deploy_steps))
        download = next(s for s in deploy_steps if s.get("download") == "current")
        self.assertEqual(download["artifact"], publish["inputs"]["artifact"])

    def test_deploy_source_uses_queue_time_branch_parameter(self):
        self.assertEqual(self.pipeline["resources"]["repositories"], [{
            "repository": "deploySource", "type": "github",
            "endpoint": "github.com_YauheniPo", "name": "YauheniPo/popot-bot-2.0",
            "ref": "refs/heads/${{ parameters.deployBranch }}",
        }])
        parameters = {p["name"]: p for p in self.pipeline["parameters"]}
        self.assertEqual(parameters["deployBranch"]["default"], "main")
        self.assertEqual(self.pipeline["resources"]["repositories"][0]["ref"],
                         "refs/heads/${{ parameters.deployBranch }}")
        self.assertEqual(parameters["deployBranch"]["type"], "string")
        variables = self.pipeline["stages"][0]["jobs"][0]["variables"]
        self.assertEqual(variables["deploymentSourceRef"], "$[ resources.repositories.deploySource.ref ]")
        self.assertEqual(variables["deploymentSourceVersion"], "$[ resources.repositories.deploySource.version ]")

    def test_mode_parameters_reach_both_ansible_commands_after_vault(self):
        parameters = {p["name"]: p for p in self.pipeline["parameters"]}
        self.assertEqual(parameters["deployBranch"]["type"], "string")
        self.assertEqual(parameters["deployBranch"]["default"], "main")
        self.assertEqual(set(parameters["deployMode"]["values"]), {"full", "config-only", "runtime-only"})
        steps = self.pipeline["stages"][1]["jobs"][0]["strategy"]["runOnce"]["deploy"]["steps"]
        commands = [s for s in steps if "ansible-playbook \\" in s.get("bash", "")]
        self.assertEqual(len(commands), 2)
        for step in commands:
            script = step["bash"]
            self.assertGreater(script.index('hermes_deploy_mode=${DEPLOY_MODE}'), script.index('@${HERMES_VAULT_FILE}'))
            self.assertEqual(step["env"]["DEPLOY_MODE"], "${{ parameters.deployMode }}")
            with tempfile.TemporaryDirectory() as temp:
                for mode in parameters["deployMode"]["values"]:
                    # Execute the real pipeline shell, replacing only the external
                    # Ansible command so tests cannot connect to a host.
                    result = subprocess.run(
                        ["bash", "-c", "ansible-playbook() { printf '%s\\n' \"$@\"; }\n" + script],
                        text=True, capture_output=True, check=True,
                        env={**os.environ, "DEPLOY_MODE": mode,
                             "ANSIBLE_LOCAL_TEMP": temp, "ANSIBLE_REMOTE_TEMP": temp,
                             "HERMES_VAULT_FILE": f"{temp}/vault with spaces",
                             "HERMES_VAULT_PASSWORD_FILE": f"{temp}/password"},
                    )
                    args = result.stdout.splitlines()
                    self.assertIn(f"@{temp}/vault with spaces", args)
                    self.assertNotIn("--private-key", args)
                    self.assertEqual(args[-3:], ["--extra-vars", f"hermes_deploy_mode={mode}",
                                                 "hermes/ansible/playbook.yml"])


if __name__ == "__main__":
    unittest.main()
