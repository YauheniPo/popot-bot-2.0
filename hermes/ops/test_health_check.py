"""Execute the load check with deterministic host inputs and awk failures."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).with_name("health-check.sh").read_text()
LOAD_CHECK = SCRIPT.split('cpu_count="', 1)[1].split('\nif [[ -f "${HERMES_METRICS_FILE}"', 1)[0]
LOAD_CHECK = 'cpu_count="' + LOAD_CHECK


class HealthLoadTests(unittest.TestCase):
    def run_check(self, load="4", failure=""):
        harness = r'''
set -Eeuo pipefail
HERMES_LOAD_WARN_PER_CPU=2
add_issue() { printf '%s\n' "$1"; }
is_number() { [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]; }
getconf() { printf '2\n'; }
awk() {
    if [[ "$*" == *'/proc/loadavg'* ]]; then
        printf '%s\n' "$TEST_LOAD"
        return
    fi
    # Reproduce the GNU awk reserved-name rejection on any controller OS.
    if [[ "$*" == *' load='* ]]; then return 2; fi
    if [[ "$TEST_FAILURE" == compare && "$*" == *'exit '* ]]; then return 2; fi
    if [[ "$TEST_FAILURE" == calculate && "$*" == *'cpus='* ]]; then return 2; fi
    "$REAL_AWK" "$@"
}
'''
        return subprocess.run(["bash", "-c", harness + LOAD_CHECK], capture_output=True,
                              text=True, timeout=5, env={**os.environ,
                              "TEST_LOAD": load, "TEST_FAILURE": failure,
                              "REAL_AWK": shutil.which("awk")})

    def test_load_below_at_and_above_threshold(self):
        for load, expected in (("3.9", ""), ("4", "load\n"), ("4.1", "load\n")):
            with self.subTest(load=load):
                result = self.run_check(load)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_awk_errors_are_reported_instead_of_silently_healthy(self):
        for failure in ("compare", "calculate"):
            with self.subTest(failure=failure):
                result = self.run_check(failure=failure)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "load-check\n")

    def test_missing_or_malformed_load_is_reported(self):
        for load in ("", "invalid"):
            with self.subTest(load=load):
                result = self.run_check(load)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "load-check\n")

    def test_maintenance_expires_and_does_not_overwrite_previous_health_state(self):
        prefix = SCRIPT.split("\nfor threshold", 1)[0]
        for age, suppressed in ((0, True), (120, False), (-120, False)):
            with self.subTest(age=age), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "ops").mkdir()
                state = root / "ops" / "health-state"
                state.write_text("existing-issue\n")
                marker = root / "ops" / "gateway-maintenance"
                marker.touch()
                timestamp = time.time() - age
                os.utime(marker, (timestamp, timestamp))
                result = subprocess.run(["bash", "-c", 'logger() { echo suppressed; }\n' + prefix + '\necho checks'],
                                        capture_output=True, text=True, timeout=5,
                                        env={**os.environ, "HERMES_HOME": str(root),
                                             "HERMES_GATEWAY_MAINTENANCE_MAX_AGE_SECONDS": "60"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "suppressed\n" if suppressed else "checks\n")
                self.assertEqual(state.read_text(), "existing-issue\n")
