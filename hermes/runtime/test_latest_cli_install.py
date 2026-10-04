"""Exercise deploy CLI selection with real shell and no network or installation."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LatestCliInstallTests(unittest.TestCase):
    def exercise(self, component, installed, resolved='2.3.4'):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            node = home / 'node'
            (node / 'bin').mkdir(parents=True)
            npm = node / 'bin/npm'
            npm.touch()
            npm.chmod(0o755)
            package = '@googleworkspace/cli' if component == 'gws' else 'agent-browser'
            manifest = node / 'lib/node_modules' / package / 'package.json'
            manifest.parent.mkdir(parents=True)
            manifest.write_text(json.dumps({'version': installed}))
            capture = home / 'calls'
            if component == 'gws':
                body = 'install_google_workspace_cli'
            else:
                source = (ROOT / 'ops/install-browser-automation.sh').read_text()
                body = source.split('installed_browser_version=', 1)[1].split('log "writing persistent', 1)[0]
                body = 'installed_browser_version=' + body
                # Include the resolver immediately before inspecting the installed package.
                resolver = source.split('resolve_browser_version() {', 1)[1].split('\n}\n', 1)[0] if 'resolve_browser_version() {' in source else ''
                # Wrap resolver in a function call since test runs it inline
                body = 'resolve_browser_version() {\n' + resolver + '\n}\nresolve_browser_version\n' + body
            script = f'''
set -Eeuo pipefail
source {shlex.quote(str(ROOT / 'deploy/runtime.sh'))}
SCRIPT_DIR={shlex.quote(str(ROOT))}
VPS_CONFIG_APPLIER="$SCRIPT_DIR/runtime/apply-config.py"
CONFIG_APPLIER="$VPS_CONFIG_APPLIER"
VPS_SETTINGS_FILE="$SCRIPT_DIR/config/vps-defaults.yml"
HERMES_HOME={shlex.quote(str(home))}
HERMES_NODE_BIN={shlex.quote(str(node / 'bin'))}
HERMES_INSTALL_DIR="$HERMES_HOME/absent-hermes"
NODE_BIN="$HERMES_NODE_BIN" NPM_BIN="$NODE_BIN/npm"
USER_HOME="$HERMES_HOME" INSTALL_GOOGLE_CLI=true AGENT_BROWSER_VERSION=latest
log() {{ :; }}
warn() {{ :; }}
die() {{ echo "$*" >&2; exit 1; }}
python3() {{
  if [[ "$1" == *resolve-tool-version.py ]]; then printf '%s\\n' {shlex.quote(resolved)}; return; fi
  command python3 "$@"
}}
run_as_hermes() {{
  if [[ "$1" == "$NODE_BIN/npm" ]]; then printf '%s\\n' "$@" >> {shlex.quote(str(capture))}; fi
}}
{body}
'''
            # gws and browser use HERMES_HOME/node by convention.
            result = subprocess.run(['bash', '-c', script], env=os.environ.copy(), text=True,
                                    capture_output=True, timeout=5)
            calls = capture.read_text() if capture.exists() else ''
            return result, calls

    def test_latest_is_resolved_to_exact_version_before_install(self):
        for component, package in [('gws', '@googleworkspace/cli'), ('browser', 'agent-browser')]:
            with self.subTest(component=component):
                result, calls = self.exercise(component, '1.0.0')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(package + '@2.3.4', calls)
                self.assertNotIn('@latest', calls)

    def test_matching_latest_is_not_reinstalled(self):
        for component in ('gws', 'browser'):
            with self.subTest(component=component):
                result, calls = self.exercise(component, '2.3.4')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, '')

    def test_invalid_resolved_version_prevents_install(self):
        for component in ('gws', 'browser'):
            with self.subTest(component=component):
                result, calls = self.exercise(component, '1.0.0', 'broken')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, '')
