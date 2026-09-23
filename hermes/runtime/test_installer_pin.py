"""Exercise pinned updates with real Git, without running host installation."""

import contextlib
import io
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "runtime/prepare-hermes-installer.py"
# Minimal upstream clone_repo fixture; the opt-in suite also runs the exact
# function from the pinned checkout with the same real-Git scenarios.
FIXTURE = '''clone_repo() {
    cd "$INSTALL_DIR"
            local autostash_ref=""
    if [ -n "$(git ls-files --unmerged)" ]; then
                    git reset -q
    fi
    if [ -n "$(git status --porcelain)" ]; then
        git stash push --include-untracked -m hermes-install-autostash-test
        autostash_ref="stash@{0}"
    fi
            git remote set-branches origin "$BRANCH" 2>/dev/null || true
            git fetch origin "$BRANCH"
            git checkout "$BRANCH"
            # Managed installs should follow origin/$BRANCH exactly. If the
            # checkout has diverged (or has local-only commits), ff-only pull
            # cannot succeed — mirror ``hermes update`` and reset to the
            # fetched remote so bootstrap/install can recover.
            if ! git pull --ff-only origin "$BRANCH"; then
                log_warn "Fast-forward not possible; resetting managed install to origin/$BRANCH..."
                git reset --hard "origin/$BRANCH"
            fi
    if [ -n "$autostash_ref" ]; then
        if git stash apply "$autostash_ref"; then
            git stash drop "$autostash_ref"
        else
                        git reset --hard HEAD >/dev/null 2>&1 || true
                        log_info "Working tree reset to clean state."
                        log_info "Restore your changes later with: git stash apply $autostash_ref"
        fi
    fi
    git checkout --detach "$INSTALL_COMMIT"
}
'''


class InstallerPinTests(unittest.TestCase):
    source = FIXTURE

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
                    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "gateway.py").write_text("old upstream\n" + "context\n" * 20 + "default\n")
        self.git(self.origin, "add", ".")
        self.git(self.origin, "commit", "-qm", "pinned")
        self.pin = self.git(self.origin, "rev-parse", "HEAD").strip()
        self.repo = self.root / "checkout"
        self.git(self.root, "clone", "-q", str(self.origin), str(self.repo))
        (self.origin / "gateway.py").write_text("new upstream\n" + "context\n" * 20 + "default\n")
        self.git(self.origin, "commit", "-qam", "main advanced")
        self.latest = self.git(self.origin, "rev-parse", "HEAD").strip()
        self.installer = self.root / "installer.sh"
        self.installer.write_text(self.source)

    def git(self, directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args],
                                       env=self.env, text=True, stderr=subprocess.PIPE)

    def prepare(self):
        return self.run_prepare([str(self.installer)])

    def run_prepare(self, arguments):
        # Run the real CLI in-process so CI coverage includes the preparer,
        # while checkout/stash behavior below still executes real Git/Bash.
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", [str(PREPARE), *arguments]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as result:
                runpy.run_path(str(PREPARE), run_name="__main__")
        return subprocess.CompletedProcess(arguments, result.exception.code, stdout.getvalue(), stderr.getvalue())

    def run_install(self, pin=None):
        script = f'''set -euo pipefail
log_info() {{ :; }}
log_warn() {{ :; }}
log_success() {{ :; }}
log_error() {{ printf '%s\\n' "$*" >&2; }}
discard_update_lockfile_churn() {{ :; }}
INSTALL_DIR={shlex.quote(str(self.repo))}
INSTALL_COMMIT={pin or self.pin}
BRANCH=main FORCE_COMMIT=true
source {shlex.quote(str(self.installer))}
clone_repo
'''
        return subprocess.run(["bash", "-c", script], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20)

    def test_patched_checkout_stays_pinned_and_preserves_local_files_on_retry(self):
        local = "old upstream\n" + "context\n" * 20 + "managed patch\n"
        (self.repo / "gateway.py").write_text(local)
        (self.repo / "personal.txt").write_text("personal file")
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        for _ in range(2):
            result = self.run_install()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").strip(), self.pin)
            self.assertEqual((self.repo / "gateway.py").read_text(), local)
            self.assertEqual((self.repo / "personal.txt").read_text(), "personal file")
            self.assertEqual(self.git(self.repo, "stash", "list"), "")

    def test_recovers_checkout_left_on_new_main(self):
        self.git(self.repo, "pull", "--ff-only")
        (self.repo / "gateway.py").write_text("new upstream\n" + "context\n" * 20 + "managed patch\n")
        self.assertEqual(self.prepare().returncode, 0)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.repo / "gateway.py").read_text(),
                         "old upstream\n" + "context\n" * 20 + "managed patch\n")
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").strip(), self.pin)

    def test_conflict_fails_closed_and_preserves_stash_on_retry(self):
        self.git(self.repo, "pull", "--ff-only")
        (self.repo / "gateway.py").write_text("conflicting personal edit\n")
        self.assertEqual(self.prepare().returncode, 0)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        stash = self.git(self.repo, "rev-parse", "refs/stash").strip()
        self.assertEqual(self.git(self.repo, "show", f"{stash}:gateway.py"), "conflicting personal edit\n")
        self.assertTrue(self.git(self.repo, "ls-files", "--unmerged"))
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/stash").strip(), stash)

    def test_missing_commit_preserves_changes_in_stash_and_does_not_update_main(self):
        (self.repo / "personal.txt").write_text("keep me")
        self.assertEqual(self.prepare().returncode, 0)
        result = self.run_install("f" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").strip(), self.pin)
        self.assertEqual(self.git(self.repo, "show", "stash@{0}^3:personal.txt"), "keep me")
        stash = self.git(self.repo, "rev-parse", "refs/stash")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/stash"), stash)

    def test_clean_checkout_updates_to_requested_commit(self):
        self.assertEqual(self.prepare().returncode, 0)
        result = self.run_install(self.latest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD").strip(), self.latest)

    def test_unrelated_user_stash_is_untouched(self):
        (self.repo / "personal.txt").write_text("saved by operator")
        self.git(self.repo, "stash", "push", "-u", "-m", "personal work")
        stash = self.git(self.repo, "rev-parse", "refs/stash")
        (self.repo / "gateway.py").write_text("old upstream\n" + "context\n" * 20 + "managed patch\n")
        self.assertEqual(self.prepare().returncode, 0)
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.repo, "rev-parse", "refs/stash"), stash)

    def test_unmerged_index_without_installer_stash_is_not_reset(self):
        self.git(self.repo, "checkout", "-qb", "personal")
        (self.repo / "gateway.py").write_text("personal committed edit\n")
        self.git(self.repo, "commit", "-qam", "personal")
        self.git(self.repo, "fetch", "origin")
        result = subprocess.run(["git", "-C", str(self.repo), "merge", "origin/main"],
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        unmerged = self.git(self.repo, "ls-files", "--unmerged")
        self.assertTrue(unmerged)
        self.assertEqual(self.prepare().returncode, 0)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.repo, "ls-files", "--unmerged"), unmerged)
        self.assertEqual(self.git(self.repo, "stash", "list"), "")

    def test_prepare_is_idempotent_and_rejects_unrecognized_installer_without_writing(self):
        self.assertEqual(self.prepare().returncode, 0)
        prepared = self.installer.read_bytes()
        subprocess.run(["bash", "-n", str(self.installer)], check=True, timeout=10)
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.installer.read_bytes(), prepared)
        for source in ("unknown installer\n", self.source.replace("git reset -q", "git reset --quiet")):
            self.installer.write_text(source)
            result = self.prepare()
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.installer.read_text(), source)

    def test_prepare_cli_rejects_missing_arguments_and_files(self):
        for arguments, code in (([], 2), ([str(self.root / "missing.sh")], 1)):
            result = self.run_prepare(arguments)
            self.assertEqual(result.returncode, code)


@unittest.skipUnless(os.environ.get("HERMES_UPSTREAM_DIR"), "HERMES_UPSTREAM_DIR is not configured")
class PinnedUpstreamInstallerTests(InstallerPinTests):
    def setUp(self):
        source = (Path(os.environ["HERMES_UPSTREAM_DIR"]) / "scripts/install.sh").read_text()
        self.source = "clone_repo() {" + source.split("\nclone_repo() {", 1)[1].split("\nsetup_venv()", 1)[0]
        super().setUp()
