"""Exercise the deploy downloader with real curl and a loopback HTTP fixture."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = b"#!/bin/sh\necho fixture\n"


@unittest.skipUnless(shutil.which("curl"), "curl is required")
class InstallerDownloadTests(unittest.TestCase):
    def run_download(self, statuses, *, checksum=None, retry_after="1"):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = statuses[min(len(requests), len(statuses) - 1)]
                requests.append(time.monotonic())
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", retry_after)
                self.end_headers()
                self.wfile.write(INSTALLER if status == 200 else b"untrusted error body")

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with tempfile.TemporaryDirectory() as temp:
                    target = Path(temp) / "installer"
                    arguments = Path(temp) / "curl-args"
                    curl_wrapper = Path(temp) / "curl"
                    # An executable wrapper behaves like curl on the VPS, without
                    # inheriting Bash 3.2's ERR trap into a shell-function mock.
                    curl_wrapper.write_text(f"""#!/bin/bash
printf '%s\\n' "$@" > {shlex.quote(str(arguments))}
args=()
for arg in "$@"; do
  case "$arg" in
    '=https') args+=('=http') ;;
    https://raw.githubusercontent.com/*) args+=(http://127.0.0.1:{server.server_port}/installer) ;;
    *) args+=("$arg") ;;
  esac
done
exec {shlex.quote(shutil.which('curl'))} "${{args[@]}}"
""")
                    curl_wrapper.chmod(0o700)
                    deploy = (ROOT / "deploy-hermes.sh").read_text()
                    main = "main() {" + deploy.split("\nmain() {", 1)[1].rsplit("\nmain\n", 1)[0]
                    cleanup = "cleanup() {" + deploy.split("\ncleanup() {", 1)[1].split("\nvalidate_inputs()", 1)[0]
                    # Run the actual main ordering, download and EXIT cleanup;
                    # replace host mutations, never invoke services or installers.
                    stubs = "\n".join(
                        f"{name}() {{ :; }}" for name in (
                            "resolve_source_pin", "validate_inputs", "install_host_dependencies",
                            "install_tailscale", "ensure_service_user", "resolve_user_paths",
                            "resolve_managed_runtime", "enable_host_administration",
                        )
                    )
                    script = f"""
set -Eeuo pipefail
source {shlex.quote(str(ROOT / 'deploy/runtime.sh'))}
log() {{ printf '%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ warn "$*"; exit 1; }}
INSTALLER_FILE=''
GATEWAY_WAS_QUIESCED=false
UPDATE_MUTATION_STARTED=false
HERMES_RAW_BASE_URL=https://raw.githubusercontent.com/NousResearch/hermes-agent
HERMES_COMMIT={'a' * 40}
INSTALLER_SHA256={checksum or hashlib.sha256(INSTALLER).hexdigest()}
mktemp() {{ printf '%s\\n' {shlex.quote(str(target))}; touch {shlex.quote(str(target))}; }}
{cleanup}
{stubs}
quiesce_existing_gateway_for_update() {{ echo GATEWAY_STOP; }}
backup_existing_installation() {{ echo BACKUP; }}
install_hermes() {{ echo INSTALL; exit 0; }}
{main}
main
"""
                    result = subprocess.run(
                        ["bash", "-c", script], capture_output=True, text=True, timeout=20,
                        env={**os.environ, "PATH": temp + os.pathsep + os.environ["PATH"]},
                    )
                    self.assertFalse(target.exists(), "temporary download must be cleaned up")
                    return result, requests, arguments.read_text().splitlines()
            finally:
                server.shutdown()
                worker.join(timeout=2)

    def test_429_retries_then_verifies_before_stopping_gateway(self):
        result, requests, _ = self.run_download([429, 200])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(requests[1] - requests[0], 0.9)
        self.assertLess(result.stdout.index("Installer checksum verified"), result.stdout.index("GATEWAY_STOP"))
        self.assertLess(result.stdout.index("GATEWAY_STOP"), result.stdout.index("BACKUP"))
        self.assertLess(result.stdout.index("BACKUP"), result.stdout.index("INSTALL"))

    def test_exhausted_429_keeps_gateway_running(self):
        result, requests, _ = self.run_download([429])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(requests), 4)
        self.assertNotIn("GATEWAY_STOP", result.stdout)
        self.assertNotIn("BACKUP", result.stdout)
        self.assertIn("http_status=429", result.stderr)
        self.assertNotIn("untrusted error body", result.stderr)

    def test_404_is_not_retried(self):
        result, requests, _ = self.run_download([404])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("GATEWAY_STOP", result.stdout)
        self.assertIn("curl_exit=22", result.stderr)
        self.assertIn("http_status=404", result.stderr)

    def test_checksum_mismatch_keeps_gateway_running(self):
        result, requests, _ = self.run_download([200], checksum="0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(requests), 1)
        self.assertIn("installer checksum mismatch", result.stderr)
        self.assertNotIn("GATEWAY_STOP", result.stdout)

    def test_download_has_bounded_timeouts_and_preserves_https(self):
        result, _, args = self.run_download([200])
        self.assertEqual(result.returncode, 0, result.stderr)
        for option, value in (
            ("--connect-timeout", "10"), ("--max-time", "30"),
            ("--retry", "3"), ("--retry-max-time", "120"), ("--proto", "=https"),
        ):
            with self.subTest(option=option):
                self.assertIn(option, args)
                self.assertEqual(args[args.index(option) + 1], value)
        self.assertNotIn("--insecure", args)
        self.assertNotIn("--retry-all-errors", args)

    def test_retry_after_beyond_budget_fails_without_waiting(self):
        result, requests, _ = self.run_download([429], retry_after="3600")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(requests), 1)
        self.assertNotIn("GATEWAY_STOP", result.stdout)


if __name__ == "__main__":
    unittest.main()
