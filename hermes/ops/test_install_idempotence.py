"""Exercise provisioning decisions without root, systemd or network access."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


INSTALL = Path(__file__).parent / "install"


class OpsInstallIdempotenceTests(unittest.TestCase):
    def run_shell(self, root, body, modules=("common",)):
        sources = "\n".join((INSTALL / f"{name}.sh").read_text() for name in modules)
        # Redirect absolute managed paths into the test sandbox. The functions
        # themselves run unchanged; external/root operations are test doubles.
        for prefix in ("/etc/", "/var/lib/", "/usr/share/", "/usr/local/"):
            sources = sources.replace(prefix, str(root) + prefix)
        script = f"""
set -euo pipefail
cd {shlex.quote(str(root))}
OPS_CHANGED=false
OPS_RESTART_DIR="$PWD/pending"
HERMES_HOME="$PWD/home"
USER_HOME="$PWD"
HERMES_USER=test
HERMES_GROUP=test
PLUGIN_CONFIGURATION_CHANGED=false
START_TIMERS=true
RESTART_SERVICES=true
SCRIPT_DIR="$PWD/bundle/ops"
mkdir -p "$HERMES_HOME"
{sources}
log() {{ :; }}
die() {{ echo "$*" >&2; exit 1; }}
managed_services() {{
  case "$1" in
    gateway) echo hermes-gateway.service;;
    observability) printf '%s\n' hermes-prometheus.service grafana-server.service;;
    timers) echo hermes-metrics.timer;;
  esac
}}
managed_value() {{
  case "$1" in
    vps_packages.apt_retry_attempts) echo 2;;
    vps_packages.apt_retry_delay_seconds) echo 1;;
    vps_packages.grafana_channel) echo stable;;
    *) echo test;;
  esac
}}
chown() {{ :; }}
stat() {{
  {shlex.quote(sys.executable)} -c 'import os,sys; path=sys.argv[-1]; owner="root:root" if "/usr/local/lib/hermes-ops/" in path else "test:test"; print(format(os.stat(path).st_mode & 0o777, "o") + ":" + owner)' "$@"
}}
install() {{
  printf '%s\n' "$*" >> installs
  local mode=644
  while (($#)); do
    case "$1" in
      -o|-g) shift 2;;
      -m) mode="$2"; shift 2;;
      -d) shift; while [[ "${{1:-}}" == -* ]]; do shift 2; done; mkdir -p "$@"; return;;
      *) break;;
    esac
  done
  if [[ -L "$2" ]]; then rm "$2"; fi
  cp "$1" "$2"
  chmod "$mode" "$2"
}}
systemctl() {{ printf '%s\n' "$*" >> systemctl-calls; }}
apt-get() {{ echo 'unexpected apt-get' >&2; return 99; }}
{body}
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=15, env={**os.environ, "LC_ALL": "C"})

    def test_review_sync_install_and_migration_do_not_restart_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("ops/sync-ai-review-metrics.py", "ops/cron/hermes-ai-review-metrics-sync.py",
                         "runtime/github-cli-wrapper.py"):
                fixture = root / "bundle" / name
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text("# fixture " + name)
            result = self.run_shell(root, """
timeout() { shift; "$@"; }
runuser() {
  printf '%s\n' "$*" >> migration-calls
  echo review-metrics-job-changed=false
}
install_review_metrics_sync
test "$OPS_CHANGED" = true
test -f "$HERMES_HOME/scripts/hermes-ai-review-metrics-sync.py"
test -f "$PWD/usr/local/lib/hermes-ops/sync-ai-review-metrics.py"
test -f "$PWD/usr/local/lib/hermes-ops/github-cli-wrapper.py"
OPS_CHANGED=false
install_review_metrics_sync
test "$OPS_CHANGED" = false
runuser() { echo review-metrics-job-changed=true; }
install_review_metrics_sync
test "$OPS_CHANGED" = true
test ! -d "$OPS_RESTART_DIR"
""", ("common", "assets"))
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "migration-calls").read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertIn("-u test -- env", calls[0])
            self.assertIn("apply-config.py review-metrics-job --settings", calls[0])
            self.assertEqual(len((root / "installs").read_text().splitlines()), 9)

    def test_unchanged_file_is_not_replaced_and_pending_restart_survives_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = """
printf original > source
install_managed_file source target 0644 test test hermes-prometheus.service
test -f "$OPS_RESTART_DIR/hermes-prometheus.service"
OPS_CHANGED=false
install_managed_file source target 0644 test test hermes-prometheus.service
test "$OPS_CHANGED" = false
test "$(grep -c -- '-m 0644' installs)" -eq 1
report_ops_changes
"""
            result = self.run_shell(root, body)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ops-restart=hermes-prometheus.service", result.stdout)
            # A different invocation still sees the restart after a failed deploy.
            result = self.run_shell(root, "report_ops_changes")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ops-restart=hermes-prometheus.service", result.stdout)

    def test_restart_names_preserve_configured_unit_syntax_and_reject_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, 'mark_ops_change hermes:custom@profile.service')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'pending/hermes:custom@profile.service').exists())
            result = self.run_shell(root, 'mark_ops_change ../outside.service')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'outside.service').exists())

    def test_changed_file_and_failed_copy_require_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").write_text("new")
            (root / "target").write_text("old")
            result = self.run_shell(root, """
install() {
  if [[ "$1" == '-d' ]]; then mkdir -p "$OPS_RESTART_DIR"; else return 9; fi
}
install_managed_file source target 0644 test test hermes-prometheus.service
""")
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "pending/hermes-prometheus.service").exists())
            self.assertEqual((root / "target").read_text(), "old")

    def test_permissions_are_repaired_and_symlink_is_replaced_without_touching_referent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, """
printf same > source
cp source target
chmod 0600 target
install_managed_file source target 0644 test test hermes-prometheus.service
test "$OPS_CHANGED" = true
OPS_CHANGED=false
printf same > referent
ln -s referent link
install_managed_file source link 0644 test test grafana-server.service
test ! -L link
test -f "$OPS_RESTART_DIR/grafana-server.service"
""")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "target").stat().st_mode & 0o777, 0o644)
            self.assertEqual((root / "referent").read_text(), "same")

    def test_missing_package_is_installed_and_failure_remains_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for fail in (True, False):
                with self.subTest(fail=fail):
                    result = self.run_shell(root, f"""
dpkg-query() {{
  [[ "${{@: -1}}" != prometheus ]] || return 1
  printf 'install ok installed'
}}
apt_get_retry() {{
  printf '%s\\n' "$*" >> apt-calls
  [[ "$1" != install ]] || return {9 if fail else 0}
}}
curl() {{ printf key > "${{@: -1}}"; }}
render() {{ :; }}
install_grafana_password_file() {{ :; }}
install_grafana_sqlite_plugin() {{ :; }}
chmod() {{ :; }}
mkdir -p etc/apt/sources.list.d
install_observability_dependencies
""", ("common", "packages"))
                    self.assertEqual(result.returncode, 9 if fail else 0, result.stderr)
                    self.assertTrue((root / "pending/hermes-prometheus.service").exists())
            calls = (root / "apt-calls").read_text().splitlines()
            self.assertEqual(calls, ["update", "install -y --no-install-recommends prometheus"] * 2)

    def test_first_timer_setup_still_runs_and_checks_initial_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, """
systemctl() {
  printf '%s\n' "$*" >> systemctl-calls
  case "$*" in
    'is-enabled --quiet hermes-backup.timer') return 1;;
    'start hermes-backup.service') return 7;;
  esac
}
reconcile_observability_services
""", ("common", "services"))
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertNotIn("enable --now hermes-metrics.timer", (root / "systemctl-calls").read_text())

    def test_new_grafana_credentials_require_restart_without_printing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, """
mkdir -p etc
openssl() { printf 'fake-test-password'; }
install_grafana_password_file
test -f "$OPS_RESTART_DIR/grafana-server.service"
""", ("common", "packages"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('fake-test-password', result.stdout + result.stderr)

    def test_deferred_timers_keep_their_pending_restarts_on_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, """
mark_ops_change grafana-server.service
mark_ops_change hermes-gateway.service
mark_ops_change hermes-metrics.timer
START_TIMERS=false
acknowledge_ops_restarts
test ! -e "$OPS_RESTART_DIR/grafana-server.service"
test -f "$OPS_RESTART_DIR/hermes-gateway.service"
test -f "$OPS_RESTART_DIR/hermes-metrics.timer"
""")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_restart_keeps_pending_marker_and_does_not_acknowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, """
mark_ops_change grafana-server.service
systemctl() {
  printf '%s\n' "$*" >> systemctl-calls
  [[ "$*" != 'restart grafana-server.service' ]]
}
reconcile_observability_services
""", ("common", "services"))
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "pending/grafana-server.service").exists())

    def test_only_changed_service_restarts_and_no_scheduled_backup_is_forced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_shell(root, """
mark_ops_change grafana-server.service
reconcile_observability_services
reconcile_observability_services
""", ("common", "services"))
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (root / "systemctl-calls").read_text().splitlines()
            self.assertEqual([call for call in calls if call.startswith("restart ")],
                             ["restart grafana-server.service"])
            self.assertNotIn("start hermes-backup.service", calls)

    def test_present_packages_and_repository_do_not_contact_apt_or_key_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "etc/apt/keyrings/grafana.asc"
            key.parent.mkdir(parents=True)
            key.write_text("existing key")
            repo = root / "etc/apt/sources.list.d/grafana.list"
            repo.parent.mkdir(parents=True)
            repo.write_text(f"deb [signed-by={key}] https://apt.grafana.com stable main\n")
            result = self.run_shell(root, """
dpkg-query() { printf 'install ok installed'; }
apt_get_retry() { echo 'unexpected apt' >&2; return 99; }
curl() { echo 'unexpected download' >&2; return 99; }
render() { :; }
install_grafana_password_file() { :; }
install_grafana_sqlite_plugin() { :; }
chmod() { :; }
install_observability_dependencies
""", ("common", "packages"))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_plugin_second_install_does_not_require_restart_or_missing_legacy_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / 'bundle/ops'
            shutil.copytree(INSTALL.parent / 'plugin/ops-observability', bundle / 'plugin/ops-observability')
            (bundle / 'templates').mkdir()
            (bundle / 'templates/model-prices.json').write_text('{}')
            result = self.run_shell(root, """
BACKUP_DIR="$PWD/backups"
HERMES_BIN="$PWD/missing-hermes"
install_observability_plugin
test -f "$OPS_RESTART_DIR/hermes-gateway.service"
acknowledge_ops_restarts
OPS_CHANGED=false
install_observability_plugin
report_ops_changes
""", ("common", "plugin"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ops-changed=false')
            installed = root / 'home/plugins/ops-observability'
            self.assertEqual(sorted(p.name for p in installed.iterdir()),
                             ['__init__.py', 'billing.py', 'hooks.py', 'plugin.yaml', 'privacy.py', 'storage.py'])

    def test_superpowers_preserves_unchanged_skills_and_restarts_after_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'upstream'
            (source / '.hermes-plugin').mkdir(parents=True)
            for name in ('plugin.yaml', '__init__.py'):
                (source / '.hermes-plugin' / name).write_text('fixture')
            (source / 'skills/example').mkdir(parents=True)
            skill = source / 'skills/example/SKILL.md'
            skill.write_text('original')
            body = """
HERMES_BIN="$PWD/missing-hermes"
git() { cp -R "$PWD/upstream" "${@: -1}"; }
install_superpowers_plugin
report_ops_changes
acknowledge_ops_restarts
"""
            result = self.run_shell(root, body, ("common", "superpowers"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('ops-restart=hermes-gateway.service', result.stdout)
            installed = root / 'home/plugins/superpowers/skills/example/SKILL.md'
            inode = installed.stat().st_ino
            result = self.run_shell(root, body, ("common", "superpowers"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'ops-changed=false')
            self.assertEqual(installed.stat().st_ino, inode)
            skill.write_text('updated')
            result = self.run_shell(root, body, ("common", "superpowers"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('ops-restart=hermes-gateway.service', result.stdout)
            self.assertEqual(installed.read_text(), 'updated')


if __name__ == "__main__":
    unittest.main()
